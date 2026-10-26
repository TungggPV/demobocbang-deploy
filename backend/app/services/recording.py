"""Durable full-meeting playback assembled from retained PCM, without STT/AI."""

from __future__ import annotations

import asyncio
import io
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.session import Database
from app.models.enums import JobStatus, MeetingPermission, MeetingStatus
from app.models.media import Job, MediaFile, MeetingAudioChunk
from app.models.meeting import Meeting
from app.models.user import User
from app.providers.storage import MediaStorage, StorageError, StorageNotFoundError
from app.schemas.media import MediaFileResponse, RecordingResponse
from app.services.media import MediaService
from app.services.meetings import MeetingService
from app.workers import lifecycle

JOB_TYPE = "recording_assembly"
ACTIVE = {JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING}


class RecordingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest(self, meeting_id: UUID) -> Job | None:
        return await self.session.scalar(
            select(Job)
            .where(
                Job.meeting_id == meeting_id,
                Job.job_type == JOB_TYPE,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )

    async def chunks(self, meeting_id: UUID) -> list[MeetingAudioChunk]:
        return list(
            await self.session.scalars(
                select(MeetingAudioChunk)
                .where(
                    MeetingAudioChunk.meeting_id == meeting_id,
                )
                .order_by(MeetingAudioChunk.start_ms, MeetingAudioChunk.id)
            )
        )

    async def status(self, meeting_id: UUID, user: User) -> RecordingResponse:
        meeting, permission = await MeetingService(self.session).require_access(
            meeting_id,
            user,
            MeetingPermission.READ,
        )
        if meeting.source_type == "upload":
            media = await self.session.scalar(
                select(MediaFile)
                .where(
                    MediaFile.meeting_id == meeting_id,
                    MediaFile.deleted_at.is_(None),
                    MediaFile.status == "completed",
                )
                .order_by(MediaFile.created_at.desc())
                .limit(1)
            )
            return RecordingResponse(
                status="ready"
                if media
                else "not_completed"
                if meeting.status != MeetingStatus.COMPLETED
                else "unavailable",
                media=MediaFileResponse.model_validate(media) if media else None,
            )
        if meeting.status != MeetingStatus.COMPLETED:
            return RecordingResponse(status="not_completed")
        job = await self.latest(meeting_id)
        response = RecordingResponse(status="unavailable")
        if job:
            response.job = MediaService._job_response(job)
            if job.media_file_id:
                media = await self.session.get(MediaFile, job.media_file_id)
                if media is None or media.deleted_at is not None:
                    response.status = "deleted"
                    return response
                if job.status == JobStatus.COMPLETED:
                    response.status = "ready"
                    response.media = MediaFileResponse.model_validate(media)
                    gap_ms = int((job.payload or {}).get("gap_ms", 0))
                    if gap_ms:
                        response.warning = (
                            f"Băng có {gap_ms / 1000:g} giây khoảng trống giữa các lần kết nối; "
                            "được giữ im lặng để khớp thời gian transcript."
                        )
                    return response
            response.status = "pending" if job.status in ACTIVE else "failed"
        has_audio = (
            await self.session.scalar(
                select(MeetingAudioChunk.id)
                .where(
                    MeetingAudioChunk.meeting_id == meeting_id,
                )
                .limit(1)
            )
            is not None
        )
        response.can_prepare = (
            permission in {"owner", MeetingPermission.EDIT}
            and has_audio
            and response.status != "pending"
        )
        return response

    async def prepare(self, meeting_id: UUID, user: User) -> Job:
        meeting, _ = await MeetingService(self.session).require_access(
            meeting_id,
            user,
            MeetingPermission.EDIT,
        )
        await self.session.refresh(meeting, with_for_update=True)
        if meeting.source_type != "realtime" or meeting.status != MeetingStatus.COMPLETED:
            raise ApiError(
                "RECORDING_NOT_COMPLETED",
                "Chỉ chuẩn bị băng sau khi kết thúc cuộc họp ghi âm.",
                409,
            )
        job = await self.request(meeting)
        if job is None:
            raise ApiError(
                "RECORDING_UNAVAILABLE", "Không còn âm thanh gốc để tạo băng ghi âm.", 409
            )
        await self.session.commit()
        return job

    async def request(self, meeting: Meeting) -> Job | None:
        """Caller holds the meeting lock; creation commits with completion."""
        if meeting.source_type != "realtime":
            return None
        current = await self.latest(meeting.id)
        if current:
            if current.media_file_id:
                media = await self.session.get(MediaFile, current.media_file_id)
                if media is None or media.deleted_at is not None:
                    raise ApiError(
                        "RECORDING_DELETED", "Băng ghi âm đã được xóa, không tự khôi phục.", 409
                    )
            if current.status in ACTIVE or current.status == JobStatus.COMPLETED:
                return current
        chunks = await self.chunks(meeting.id)
        if not chunks:
            return None
        job = Job(
            meeting_id=meeting.id,
            job_type=JOB_TYPE,
            status=JobStatus.QUEUED,
            progress=0,
            payload={"chunks": chunk_snapshot(chunks)},
            created_at=datetime.now(UTC),
        )
        self.session.add(job)
        await self.session.flush()
        return job


def chunk_snapshot(chunks: list[MeetingAudioChunk]) -> list[dict]:
    return [
        {
            "id": str(row.id),
            "recording_id": str(row.recording_id),
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "storage_key": row.storage_key,
        }
        for row in chunks
    ]


def append_wav(output: wave.Wave_write, content: bytes, duration_ms: int) -> None:
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            if (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getcomptype(),
            ) != (1, 2, 16000, "NONE"):
                raise ValueError
            frames = audio.readframes(audio.getnframes())
            if len(frames) != duration_ms * 32:
                raise ValueError
            output.writeframesraw(frames)
    except (wave.Error, EOFError, ValueError) as exc:
        raise ApiError(
            "RECORDING_INVALID", "Một đoạn âm thanh thiếu dữ liệu hoặc sai định dạng.", 409
        ) from exc


class RecordingWorker:
    def __init__(self, database: Database, storage: MediaStorage, settings: Settings) -> None:
        self.database, self.storage, self.settings = database, storage, settings

    async def run(self, job_id: UUID) -> bool:
        async with self.database.session() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None or job.job_type != JOB_TYPE or not await lifecycle.start(session, job):
                return False
            meeting_id = job.meeting_id
            data = dict(job.payload or {})
            attempts = job.attempts
            key = f"media/meetings/{meeting_id}/playback/{job_id}.wav"
            published = False
            try:
                meeting = await session.get(Meeting, meeting_id)
                if meeting is None or meeting.status != MeetingStatus.COMPLETED:
                    raise ApiError(
                        "RECORDING_CHANGED", "Cuộc họp không còn ở trạng thái đã kết thúc.", 409
                    )
                chunks = data.get("chunks", [])
                if (
                    not chunks
                    or chunk_snapshot(await RecordingService(session).chunks(meeting_id)) != chunks
                ):
                    raise ApiError(
                        "RECORDING_CHANGED",
                        "Nguồn âm thanh đã thay đổi; hãy chuẩn bị lại băng.",
                        409,
                    )
                await session.commit()  # No DB locks held while reading storage.
                cursor, gap_ms = 0, 0
                previous_recording = None
                with tempfile.TemporaryDirectory(prefix="meeting-playback-") as directory:
                    path = Path(directory) / "recording.wav"
                    with wave.open(str(path), "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(16000)
                        for row in chunks:
                            start, end = int(row["start_ms"]), int(row["end_ms"])
                            if start < cursor:
                                raise ApiError(
                                    "RECORDING_OVERLAP",
                                    "Các bản ghi chồng lấn; không thể tự ghép an toàn.",
                                    409,
                                )
                            if (
                                end <= start
                                or end > self.settings.max_media_duration_seconds * 1000
                            ):
                                raise ApiError(
                                    "RECORDING_INVALID",
                                    "Thời lượng đoạn âm thanh không hợp lệ.",
                                    409,
                                )
                            if start > cursor:
                                if (
                                    previous_recording is None
                                    or row["recording_id"] == previous_recording
                                ):
                                    raise ApiError(
                                        "RECORDING_INCOMPLETE",
                                        "Thiếu đoạn âm thanh trong bản ghi; không thể ghép đầy đủ.",
                                        409,
                                    )
                                gap_ms += start - cursor
                                # Keep inter-connection offsets, never compress timestamps.
                                remaining = (start - cursor) * 32
                                while remaining:
                                    size = min(remaining, 160000)
                                    await asyncio.to_thread(output.writeframesraw, bytes(size))
                                    remaining -= size
                            content = await self.storage.get_bytes(str(row["storage_key"]))
                            await asyncio.to_thread(append_wav, output, content, end - start)
                            cursor, previous_recording = end, row["recording_id"]
                    await self.storage.put_file(path, key)
                    size_bytes = path.stat().st_size
                # Deletion/source changes during assembly must not resurrect audio.
                session.expire_all()
                meeting = await session.scalar(
                    select(Meeting).where(Meeting.id == meeting_id).with_for_update()
                )
                job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job is None or meeting is None:
                    return False
                if (
                    job.status != JobStatus.PROCESSING
                    or meeting.status != MeetingStatus.COMPLETED
                    or chunk_snapshot(await RecordingService(session).chunks(meeting_id)) != chunks
                ):
                    raise ApiError(
                        "RECORDING_CHANGED", "Nguồn âm thanh thay đổi trong khi chuẩn bị băng.", 409
                    )
                media = MediaFile(
                    meeting_id=meeting_id,
                    original_filename=f"ban-ghi-{meeting_id}.wav",
                    mime_type="audio/wav",
                    size_bytes=size_bytes,
                    duration_ms=cursor,
                    storage_key=key,
                    status="completed",
                )
                session.add(media)
                await session.flush()
                job.media_file_id = media.id
                job.payload = {**data, "gap_ms": gap_ms}
                await lifecycle.completed(session, job)
                published = True
                return False
            except ApiError as exc:
                await lifecycle.failed(session, job, exc.code, exc.message, False)
            except StorageNotFoundError:
                await lifecycle.failed(
                    session,
                    job,
                    "RECORDING_UNAVAILABLE",
                    "Không tìm thấy một đoạn âm thanh đã lưu.",
                    False,
                )
            except Exception:
                if attempts <= self.settings.worker_max_retries:
                    await lifecycle.retrying(
                        session,
                        job,
                        "RECORDING_BUILD_FAILED",
                        "Chưa chuẩn bị được băng, đang thử lại.",
                        min_backoff_ms=self.settings.worker_retry_min_backoff_ms,
                        max_backoff_ms=self.settings.worker_retry_max_backoff_ms,
                    )
                    return True
                await lifecycle.failed(
                    session,
                    job,
                    "RECORDING_BUILD_FAILED",
                    "Không thể chuẩn bị băng ghi âm. Hãy thử lại.",
                    True,
                )
            finally:
                if not published:
                    try:
                        await self.storage.delete(key)
                    except (OSError, StorageError):
                        pass
            return False
