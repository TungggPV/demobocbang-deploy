"""Learn reusable voiceprints only from human-confirmed, immutable meeting clips."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.session import Database
from app.models.enums import JobStatus, MeetingPermission
from app.models.media import Job
from app.models.meeting import Meeting
from app.models.transcript import CurrentSegment, Transcript
from app.models.user import User
from app.models.voice_profile import VoiceProfile
from app.providers.contracts import SpeakerEnrollmentProvider, STTProviderError
from app.providers.storage import MediaStorage, StorageError
from app.schemas.media import JobResponse
from app.schemas.voice_profile import MeetingVoiceLearningRequest
from app.services.media import MediaService
from app.services.meeting_audio import MeetingAudioReader, pcm_wav
from app.services.meetings import MeetingService
from app.services.transcripts import TranscriptService
from app.services.voice_profiles import MAX_IDENTIFIERS_PER_PROFILE, VoiceProfileService
from app.workers import lifecycle


class MeetingVoiceLearningService:
    def __init__(self, session: AsyncSession, storage: MediaStorage) -> None:
        self.session, self.storage = session, storage

    async def snapshot(
        self, meeting_id: UUID, user: User, segment_ids: list[UUID], *, edit: bool = True
    ) -> tuple[Meeting, list[CurrentSegment]]:
        meeting, _ = await MeetingService(self.session).require_access(
            meeting_id,
            user,
            MeetingPermission.EDIT if edit else MeetingPermission.READ,
        )
        ids = set(segment_ids)
        segments = list(
            await self.session.scalars(
                select(CurrentSegment)
                .join(
                    Transcript,
                    CurrentSegment.transcript_id == Transcript.id,
                )
                .where(
                    Transcript.meeting_id == meeting_id, CurrentSegment.logical_segment_id.in_(ids)
                )
                .order_by(CurrentSegment.start_ms)
                .execution_options(populate_existing=True)
            )
        )
        if len(segments) != len(ids):
            raise ApiError(
                "SEGMENT_NOT_FOUND",
                "Một đoạn đã chọn không còn trong transcript cuộc họp này.",
                404,
            )
        if len({item.speaker_id for item in segments}) != 1:
            raise ApiError(
                "VOICE_SAMPLE_MIXED_SPEAKERS", "Chỉ chọn các đoạn của cùng một người nói.", 422
            )
        for left, right in zip(segments, segments[1:], strict=False):
            if left.end_ms > right.start_ms:
                raise ApiError(
                    "VOICE_SAMPLE_OVERLAP", "Các đoạn được chọn bị chồng thời gian.", 422
                )
        # Reject known overlapping voices, even if they were not selected.
        for item in segments:
            overlap = await self.session.scalar(
                select(CurrentSegment.id)
                .where(
                    CurrentSegment.transcript_id == item.transcript_id,
                    CurrentSegment.logical_segment_id.not_in(ids),
                    CurrentSegment.start_ms < item.end_ms,
                    CurrentSegment.end_ms > item.start_ms,
                )
                .limit(1)
            )
            if overlap is not None:
                raise ApiError(
                    "VOICE_SAMPLE_OVERLAP",
                    "Đoạn này có lời nói chồng nhau; hãy chọn đoạn khác.",
                    422,
                )
        return meeting, segments

    async def audio(self, meeting: Meeting, segments: list[CurrentSegment]) -> bytes:
        reader = MeetingAudioReader(self.session, self.storage)
        intervals = []
        remaining = 25_000
        for item in segments:
            length = min(remaining, item.end_ms - item.start_ms)
            if length <= 0:
                break
            intervals.append((item.start_ms, item.start_ms + length))
            remaining -= length
        return pcm_wav(await reader.clips(meeting, intervals))

    async def request(
        self, meeting_id: UUID, user: User, payload: MeetingVoiceLearningRequest
    ) -> JobResponse:
        name = payload.name.strip()
        if not name:
            raise ApiError("INVALID_NAME", "Tên người nói không được để trống.", 422)
        meeting, segments = await self.snapshot(meeting_id, user, payload.segment_ids)
        if sum(item.end_ms - item.start_ms for item in segments) < 10_000:
            raise ApiError(
                "VOICE_PROFILE_AUDIO_TOO_SHORT",
                "Chọn thêm đoạn của cùng người nói để có ít nhất 10 giây âm thanh.",
                422,
            )
        profile_id = payload.profile_id
        if profile_id:
            await self._profile(meeting, user, profile_id)
        audio = await self.audio(meeting, segments)
        actual_duration_ms = max(0, len(audio) - 44) // 32  # pcm_wav writes a standard PCM header.
        if actual_duration_ms < 10_000:
            raise ApiError(
                "VOICE_PROFILE_AUDIO_TOO_SHORT",
                "Âm thanh thực tế chưa đủ 10 giây; hãy chọn thêm đoạn phát biểu.",
                422,
            )
        job_id = uuid4()
        key = f"media/meetings/{meeting_id}/voice-learning/{job_id}.wav"
        await self.storage.put_bytes(audio, key)
        try:
            job = Job(
                id=job_id,
                meeting_id=meeting_id,
                job_type="voice_learning",
                status=JobStatus.QUEUED,
                progress=0,
                payload={
                    "actor_id": str(user.id),
                    "name": name,
                    "profile_id": str(profile_id) if profile_id else None,
                    "clip_key": key,
                    "sample_duration_ms": actual_duration_ms,
                    "segments": [self._segment_snapshot(item) for item in segments],
                },
            )
            self.session.add(job)
            await self.session.commit()
            await self.session.refresh(job)
            return MediaService._job_response(job)
        except Exception:
            await self.session.rollback()
            await self.storage.delete(key)
            raise

    async def _profile(self, meeting: Meeting, user: User, profile_id: UUID) -> VoiceProfile:
        profile = await self.session.scalar(
            select(VoiceProfile)
            .where(
                VoiceProfile.id == profile_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # Learning may update the meeting owner's reusable library or the
        # secretary's own library, never an unrelated person's profile.
        if (
            profile is None
            or profile.user_id not in {meeting.owner_id, user.id}
            or profile.meeting_id
        ):
            raise ApiError(
                "VOICE_PROFILE_NOT_FOUND", "Không có quyền cập nhật hồ sơ giọng nói này.", 404
            )
        if profile.sample_count >= MAX_IDENTIFIERS_PER_PROFILE:
            raise ApiError(
                "VOICE_PROFILE_SAMPLE_LIMIT",
                "Hồ sơ đã có 3 mẫu. Hãy quản lý mẫu trong Mẫu giọng trước khi bổ sung.",
                409,
            )
        return profile

    @staticmethod
    def _segment_snapshot(item: CurrentSegment) -> dict[str, object]:
        return {
            "id": str(item.logical_segment_id),
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "speaker_id": str(item.speaker_id) if item.speaker_id else None,
        }


class VoiceLearningWorker:
    def __init__(
        self,
        database: Database,
        storage: MediaStorage,
        provider: SpeakerEnrollmentProvider | None,
        settings: Settings,
    ) -> None:
        self.database, self.storage, self.provider, self.settings = (
            database,
            storage,
            provider,
            settings,
        )

    async def run(self, job_id: UUID) -> bool:
        async with self.database.session() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None or not await lifecycle.start(session, job):
                return False
            payload = dict(job.payload or {})
            try:
                if self.provider is None:
                    raise ApiError(
                        "PROVIDER_CAPABILITY_UNAVAILABLE",
                        "Chưa cấu hình tạo mẫu giọng Speechmatics.",
                        422,
                    )
                actor = await session.get(User, UUID(str(payload["actor_id"])))
                if actor is None:
                    raise ApiError("AUTH_REQUIRED", "Người xác nhận không còn quyền truy cập.", 403)
                service = MeetingVoiceLearningService(session, self.storage)
                ids = [UUID(str(item["id"])) for item in payload["segments"]]
                meeting, segments = await service.snapshot(job.meeting_id, actor, ids)
                self._check_snapshot(segments, payload)
                # No meeting/segment lock is held while the provider processes audio.
                await session.commit()
                audio = await self.storage.get_bytes(str(payload["clip_key"]))
                language = meeting.language_mode if meeting.language_mode in {"vi", "en"} else "vi"
                with tempfile.TemporaryDirectory(prefix="voice-learning-") as directory:
                    path = Path(directory) / "sample.wav"
                    await asyncio.to_thread(path.write_bytes, audio)
                    enrollment = await self.provider.enroll_speaker(path, language)
                if not enrollment.speaker_identifiers:
                    raise ApiError(
                        "VOICE_PROFILE_NO_IDENTIFIER", "Không nhận được mẫu giọng hợp lệ.", 422
                    )
                # Revalidate ACL and exact intervals after the external call.
                await session.scalars(
                    select(CurrentSegment)
                    .where(
                        CurrentSegment.logical_segment_id.in_(ids),
                    )
                    .order_by(CurrentSegment.logical_segment_id)
                    .with_for_update()
                )
                await session.refresh(meeting, with_for_update=True)
                meeting, segments = await service.snapshot(job.meeting_id, actor, ids)
                self._check_snapshot(segments, payload)
                profile_id = payload.get("profile_id")
                if profile_id:
                    profile = await service._profile(meeting, actor, UUID(str(profile_id)))
                else:
                    profile = VoiceProfile(
                        user_id=meeting.owner_id,
                        meeting_id=None,
                        name=str(payload["name"]),
                        provider="speechmatics",
                        sample_duration_ms=0,
                        sample_count=0,
                        speaker_identifiers=[],
                    )
                    session.add(profile)
                    await session.flush()
                profiles = VoiceProfileService(session, storage=self.storage)
                sample_index = profile.sample_count + 1
                # Each verified clip is one sample/identifier in the reusable library.
                profiles._append_identifiers(profile, enrollment.speaker_identifiers[:1])
                profile.sample_duration_ms += int(payload["sample_duration_ms"])
                await profiles._save_sample_audio(profile, audio, sample_index)
                await TranscriptService(session).confirm_voice_identity(
                    meeting,
                    actor,
                    segments,
                    profile.id,
                    str(payload["name"]),
                )
                job.payload = {**payload, "result_profile_id": str(profile.id)}
                # Profile, manual labels, transcript revision and job success commit together.
                await lifecycle.completed(session, job)
            except STTProviderError as exc:
                if exc.retryable and job.attempts <= self.settings.worker_max_retries:
                    await lifecycle.retrying(
                        session,
                        job,
                        exc.code,
                        str(exc),
                        min_backoff_ms=self.settings.worker_retry_min_backoff_ms,
                        max_backoff_ms=self.settings.worker_retry_max_backoff_ms,
                    )
                    return True
                await lifecycle.failed(session, job, exc.code, str(exc), exc.retryable)
            except ApiError as exc:
                await lifecycle.failed(session, job, exc.code, exc.message, False)
            except Exception:
                await lifecycle.failed(
                    session,
                    job,
                    "VOICE_LEARNING_FAILED",
                    "Không thể lưu mẫu giọng từ cuộc họp.",
                    False,
                )
            try:
                await self.storage.delete(str(payload["clip_key"]))
            except StorageError:
                pass
            return False

    @staticmethod
    def _check_snapshot(segments: list[CurrentSegment], payload: dict) -> None:
        if [MeetingVoiceLearningService._segment_snapshot(item) for item in segments] != payload[
            "segments"
        ]:
            raise ApiError(
                "VOICE_SAMPLE_CHANGED",
                "Các đoạn đã được sửa trong lúc lấy mẫu; vui lòng chọn lại.",
                409,
            )
