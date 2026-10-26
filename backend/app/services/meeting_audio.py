"""Retain realtime PCM and extract bounded, timeline-aligned voice clips."""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.media import Job, MediaFile, MeetingAudioChunk
from app.models.meeting import Meeting
from app.models.transcript import TranscriptEditEvent, TranscriptVersion
from app.providers.storage import MediaStorage, StorageError

BYTES_PER_MS = 32  # 16 kHz, mono, PCM16.
CHUNK_BYTES = 5_000 * BYTES_PER_MS


def pcm_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(pcm)
    return output.getvalue()


class RealtimeAudioWriter:
    def __init__(
        self,
        session: AsyncSession,
        storage: MediaStorage,
        meeting_id: UUID,
        start_ms: int,
        max_duration_ms: int,
    ) -> None:
        self.session, self.storage, self.meeting_id = session, storage, meeting_id
        self.start_ms, self.max_duration_ms = start_ms, max_duration_ms
        self.recording_id = uuid4()
        self.buffer = bytearray()
        self.saved_bytes = 0

    @property
    def end_ms(self) -> int:
        return self.start_ms + (self.saved_bytes + len(self.buffer)) // BYTES_PER_MS

    async def append(self, content: bytes) -> None:
        if self.end_ms + len(content) // BYTES_PER_MS > self.max_duration_ms:
            raise ApiError(
                "RECORDING_DURATION_EXCEEDED", "Cuộc họp đã đạt giới hạn thời lượng ghi âm.", 422
            )
        self.buffer.extend(content)
        while len(self.buffer) >= CHUNK_BYTES:
            await self.flush(CHUNK_BYTES)

    async def flush(self, size: int | None = None) -> None:
        size = min(size or len(self.buffer), len(self.buffer))
        size -= size % BYTES_PER_MS
        if not size:
            return
        start = self.start_ms + self.saved_bytes // BYTES_PER_MS
        end = start + size // BYTES_PER_MS
        key = f"media/meetings/{self.meeting_id}/recordings/{self.recording_id}/{start}.wav"
        await self.storage.put_bytes(pcm_wav(bytes(self.buffer[:size])), key)
        self.session.add(
            MeetingAudioChunk(
                meeting_id=self.meeting_id,
                recording_id=self.recording_id,
                start_ms=start,
                end_ms=end,
                storage_key=key,
            )
        )
        # SQL expression avoids overwriting duration advanced by another final.
        await self.session.execute(
            update(Meeting)
            .where(
                Meeting.id == self.meeting_id,
                or_(Meeting.duration_ms.is_(None), Meeting.duration_ms < end),
            )
            .values(duration_ms=end)
        )
        await self.session.commit()
        del self.buffer[:size]
        self.saved_bytes += size


class MeetingAudioReader:
    def __init__(self, session: AsyncSession, storage: MediaStorage) -> None:
        self.session, self.storage = session, storage

    async def clip(self, meeting: Meeting, start_ms: int, end_ms: int) -> bytes:
        return await self.clips(meeting, [(start_ms, end_ms)])

    async def clips(self, meeting: Meeting, intervals: list[tuple[int, int]]) -> bytes:
        if (
            not intervals
            or any(not 0 <= start < end for start, end in intervals)
            or sum(end - start for start, end in intervals) > 25_000
        ):
            raise ApiError(
                "INVALID_AUDIO_INTERVAL", "Chọn các đoạn âm thanh hợp lệ, tổng tối đa 25 giây.", 422
            )
        if meeting.source_type == "realtime":
            output = bytearray()
            for start, end in intervals:
                output.extend(await self._realtime_clip(meeting.id, start, end))
            return bytes(output)
        return await self._file_clip(await self._source_media(meeting), intervals)

    async def _source_media(self, meeting: Meeting) -> MediaFile:
        # Follow manual revisions/rollbacks to the STT input, rather than guessing
        # that the latest uploaded file produced the current transcript.
        version_id = meeting.current_transcript_version_id
        seen = set()
        while version_id and version_id not in seen:
            seen.add(version_id)
            version = await self.session.get(TranscriptVersion, version_id)
            if version is None:
                break
            if version.source == "stt":
                job = await self.session.scalar(
                    select(Job)
                    .where(
                        Job.meeting_id == meeting.id,
                        Job.job_type == "media_processing",
                        Job.payload["transcript_version_id"].as_string() == str(version.id),
                    )
                    .limit(1)
                )
                if job:
                    media = await self.session.get(MediaFile, job.media_file_id)
                    if media and media.deleted_at is None and media.status == "completed":
                        return media
                    raise ApiError(
                        "MEETING_AUDIO_UNAVAILABLE", "Âm thanh nguồn của transcript đã bị xóa.", 409
                    )
                break
            rollback = await self.session.scalar(
                select(TranscriptEditEvent)
                .where(
                    TranscriptEditEvent.version_id == version.id,
                    TranscriptEditEvent.event_type == "rollback",
                )
                .limit(1)
            )
            version_id = (
                UUID(str(rollback.payload["target_version_id"]))
                if rollback
                else version.parent_version_id
            )
        # Older recordings did not store input provenance. A single retained file
        # is unambiguous; multiple files require a new verified transcription.
        media_files = list(
            await self.session.scalars(
                select(MediaFile)
                .where(
                    MediaFile.meeting_id == meeting.id,
                    MediaFile.deleted_at.is_(None),
                    MediaFile.status == "completed",
                )
                .order_by(MediaFile.created_at.desc())
            )
        )
        if len(media_files) > 1:
            raise ApiError(
                "MEETING_AUDIO_AMBIGUOUS",
                "Cuộc họp cũ có nhiều tệp âm thanh; "
                "cần xử lý lại đúng tệp để xác định nguồn transcript.",
                409,
            )
        if not media_files:
            raise ApiError(
                "MEETING_AUDIO_UNAVAILABLE", "Cuộc họp chưa có âm thanh đã xử lý để lấy mẫu.", 409
            )
        return media_files[0]

    async def _realtime_clip(self, meeting_id: UUID, start: int, end: int) -> bytes:
        chunks = list(
            await self.session.scalars(
                select(MeetingAudioChunk)
                .where(
                    MeetingAudioChunk.meeting_id == meeting_id,
                    MeetingAudioChunk.start_ms < end,
                    MeetingAudioChunk.end_ms > start,
                )
                .order_by(MeetingAudioChunk.start_ms)
            )
        )
        if any(
            left.end_ms > right.start_ms for left, right in zip(chunks, chunks[1:], strict=False)
        ):
            raise ApiError(
                "MEETING_AUDIO_OVERLAP", "Có nhiều phiên ghi âm chồng nhau tại đoạn này.", 409
            )
        cursor = start
        result = bytearray()
        for chunk in chunks:
            if chunk.start_ms > cursor:
                break
            # Overlapping recording sessions cannot be safely mixed as one voice.
            if chunk.start_ms < cursor and cursor != start:
                raise ApiError(
                    "MEETING_AUDIO_OVERLAP", "Có nhiều phiên ghi âm chồng nhau tại đoạn này.", 409
                )
            stop = min(end, chunk.end_ms)
            try:
                content = await self.storage.get_bytes(chunk.storage_key)
            except StorageError as exc:
                raise ApiError(
                    "MEETING_AUDIO_UNAVAILABLE", "Không tìm thấy âm thanh của đoạn đã chọn.", 409
                ) from exc
            with wave.open(io.BytesIO(content), "rb") as audio:
                audio.setpos((cursor - chunk.start_ms) * 16)
                result.extend(audio.readframes((stop - cursor) * 16))
            cursor = stop
            if cursor == end:
                if len(result) != (end - start) * BYTES_PER_MS:
                    raise ApiError(
                        "MEETING_AUDIO_INVALID", "Âm thanh được lưu bị thiếu dữ liệu.", 409
                    )
                return bytes(result)
        raise ApiError(
            "MEETING_AUDIO_NOT_READY",
            "Âm thanh đoạn này chưa được lưu đủ. Đợi vài giây hoặc chọn đoạn trước đó.",
            409,
        )

    async def _file_clip(self, media: MediaFile, intervals: list[tuple[int, int]]) -> bytes:
        if any(end > media.duration_ms for _, end in intervals):
            raise ApiError(
                "MEETING_AUDIO_UNAVAILABLE", "Mốc thời gian vượt quá âm thanh được lưu.", 422
            )
        with tempfile.TemporaryDirectory(prefix="meeting-clip-") as directory:
            source, output = Path(directory) / "source", Path(directory) / "clip.pcm"
            await self.storage.download_file(
                media.normalized_storage_key or media.storage_key, source
            )
            offset = min(start for start, _ in intervals)
            filters = [
                f"[0:a]atrim=start={(start - offset) / 1000}:end={(end - offset) / 1000},"
                f"asetpts=PTS-STARTPTS[a{index}]"
                for index, (start, end) in enumerate(intervals)
            ]
            filters.append(
                "".join(f"[a{index}]" for index in range(len(intervals)))
                + f"concat=n={len(intervals)}:v=0:a=1[out]"
            )
            try:
                process = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-ss",
                        str(offset / 1000),
                        "-i",
                        os.fspath(source),
                        "-filter_complex",
                        ";".join(filters),
                        "-map",
                        "[out]",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "s16le",
                        os.fspath(output),
                    ],
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            except FileNotFoundError as exc:
                raise ApiError("MEDIA_TOOL_UNAVAILABLE", "Máy chủ chưa có FFmpeg.", 503) from exc
            if process.returncode:
                raise ApiError("MEETING_AUDIO_INVALID", "Không thể trích âm thanh từ bản ghi.", 422)
            return await asyncio.to_thread(output.read_bytes)
