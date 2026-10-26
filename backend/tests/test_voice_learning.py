import asyncio
import io
import wave
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.base import Base
from app.db.session import Database
from app.models.enums import MeetingPermission
from app.models.media import Job, MediaFile, MeetingAudioChunk
from app.models.meeting import Meeting, MeetingMember
from app.models.transcript import CurrentSegment, Speaker, TranscriptEditEvent
from app.models.user import User
from app.models.voice_profile import VoiceProfile
from app.providers.contracts import SpeakerEnrollment, STTProviderError, TranscriptionSegment
from app.providers.storage import LocalStorageBackend
from app.schemas.transcript import SegmentEditRequest
from app.schemas.voice_profile import MeetingVoiceLearningRequest
from app.services.meeting_audio import MeetingAudioReader, RealtimeAudioWriter
from app.services.transcripts import TranscriptService
from app.services.voice_learning import MeetingVoiceLearningService, VoiceLearningWorker
from app.services.voice_profiles import VoiceProfileService


class Enrollment:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    async def enroll_speaker(self, source, language):
        self.calls += 1
        assert language == "vi"
        with wave.open(str(source), "rb") as audio:
            assert audio.getframerate() == 16000
            assert audio.getnframes() == 12 * 16000
        if self.error:
            raise self.error
        return SpeakerEnrollment((f"voice-{self.calls}",), 12000)


async def setup(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'voice-learning.sqlite').as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    database = Database(url)
    storage = LocalStorageBackend(tmp_path / "audio")
    async with database.session() as session:
        owner = User(email="owner@example.com", full_name="Owner", is_email_verified=True)
        outsider = User(email="outsider@example.com", full_name="Outsider", is_email_verified=True)
        session.add_all([owner, outsider])
        await session.commit()
        meeting = Meeting(
            owner_id=owner.id,
            title="Live",
            source_type="realtime",
            language_mode="vi",
            diarization_enabled=True,
            status="recording",
        )
        session.add(meeting)
        await session.commit()
        final = await TranscriptService(session).append_stt_final(
            meeting,
            owner.id,
            TranscriptionSegment(0, 12000, "Phát biểu đủ dài", "S1"),
        )
        writer = RealtimeAudioWriter(session, storage, meeting.id, 0, 120000)
        await writer.append(b"\x10\x00" * 16000 * 12)
        await writer.flush()
        return database, storage, owner.id, outsider.id, meeting.id, final.logical_segment_id


def test_realtime_audio_is_exact_and_continues_across_sessions(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        try:
            async with database.session() as session:
                meeting = await session.get(Meeting, meeting_id)
                assert meeting.duration_ms == 12000
                reader = MeetingAudioReader(session, storage)
                assert await reader.clip(meeting, 1000, 11000) == b"\x10\x00" * 160000
                later = RealtimeAudioWriter(session, storage, meeting_id, 12000, 120000)
                await later.append(b"\x20\x00" * 16000 * 5)
                await later.flush()
                clip = await reader.clip(meeting, 11000, 13000)
                assert clip == b"\x10\x00" * 16000 + b"\x20\x00" * 16000
                chunks = list(await session.scalars(select(MeetingAudioChunk)))
                assert len(chunks) == 4
                with pytest.raises(ApiError, match="chưa được lưu đủ"):
                    await reader.clip(meeting, 16000, 18000)
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_learning_is_durable_idempotent_and_reused_only_in_authorized_meetings(tmp_path):
    async def scenario():
        database, storage, owner_id, outsider_id, meeting_id, segment_id = await setup(tmp_path)
        provider = Enrollment()
        worker = VoiceLearningWorker(database, storage, provider, Settings(_env_file=None))
        try:
            async with database.session() as session:
                owner = await session.get(User, owner_id)
                queued = await MeetingVoiceLearningService(session, storage).request(
                    meeting_id,
                    owner,
                    MeetingVoiceLearningRequest(name="Chị Lan", segment_ids=[segment_id]),
                )
                assert queued.status == "queued"
            assert await worker.run(queued.id) is False
            assert await worker.run(queued.id) is False
            assert provider.calls == 1
            async with database.session() as session:
                job = await session.get(Job, queued.id)
                assert job.status == "completed"
                profile = (await session.scalars(select(VoiceProfile))).one()
                speaker = (await session.scalars(select(Speaker))).one()
                assert profile.name == speaker.name == "Chị Lan"
                assert profile.user_id == owner_id and profile.meeting_id is None
                assert speaker.voice_profile_id == profile.id
                assert profile.sample_count == 1
                assert (await session.scalars(select(TranscriptEditEvent))).one().payload[
                    "voice_learning"
                ] is True
                future = Meeting(
                    owner_id=owner_id,
                    title="Next",
                    source_type="realtime",
                    language_mode="vi",
                    diarization_enabled=True,
                )
                unrelated = Meeting(
                    owner_id=outsider_id,
                    title="Other",
                    source_type="realtime",
                    language_mode="vi",
                    diarization_enabled=True,
                )
                session.add_all([future, unrelated])
                await session.commit()
                service = VoiceProfileService(session, storage=storage)
                assert [item.name for item in await service.list_known_speakers(future.id)] == [
                    "Chị Lan"
                ]
                assert await service.list_known_speakers(unrelated.id) == ()
                outsider = await session.get(User, outsider_id)
                assert await service.list_user_profiles(outsider) == []
                with pytest.raises(ApiError):
                    await service.delete_profile(profile.id, outsider)
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["segment", "access", "provider"])
def test_learning_never_saves_changed_or_unverified_samples(tmp_path, change):
    async def scenario():
        database, storage, owner_id, outsider_id, meeting_id, segment_id = await setup(tmp_path)
        error = (
            STTProviderError(
                "Có hai người nói", code="VOICE_PROFILE_MULTIPLE_SPEAKERS", retryable=False
            )
            if change == "provider"
            else None
        )
        provider = Enrollment(error)
        try:
            async with database.session() as session:
                actor = await session.get(User, outsider_id if change == "access" else owner_id)
                if change == "access":
                    session.add(
                        MeetingMember(
                            meeting_id=meeting_id,
                            user_id=actor.id,
                            permission=MeetingPermission.EDIT,
                        )
                    )
                    await session.commit()
                queued = await MeetingVoiceLearningService(session, storage).request(
                    meeting_id,
                    actor,
                    MeetingVoiceLearningRequest(name="Lan", segment_ids=[segment_id]),
                )
                if change == "segment":
                    segment = (await session.scalars(select(CurrentSegment))).one()
                    segment.end_ms = 11000
                    await session.commit()
                if change == "access":
                    member = await session.get(MeetingMember, (meeting_id, actor.id))
                    await session.delete(member)
                    await session.commit()
            worker = VoiceLearningWorker(database, storage, provider, Settings(_env_file=None))
            assert await worker.run(queued.id) is False
            async with database.session() as session:
                job = await session.get(Job, queued.id)
                assert job.status == "failed"
                assert list(await session.scalars(select(VoiceProfile))) == []
                speaker = (await session.scalars(select(Speaker))).one()
                assert speaker.name == "Người nói 1"
                assert provider.calls == (1 if change == "provider" else 0)
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_sample_validation_enforces_acl_meeting_and_single_voice(tmp_path):
    async def scenario():
        database, storage, owner_id, outsider_id, meeting_id, segment_id = await setup(tmp_path)
        try:
            async with database.session() as session:
                service = MeetingVoiceLearningService(session, storage)
                outsider = await session.get(User, outsider_id)
                owner = await session.get(User, owner_id)
                with pytest.raises(ApiError) as denied:
                    await service.snapshot(meeting_id, outsider, [segment_id])
                assert denied.value.status_code == 403
                with pytest.raises(ApiError) as missing:
                    await service.snapshot(meeting_id, owner, [uuid4()])
                assert missing.value.code == "SEGMENT_NOT_FOUND"
                meeting = await session.get(Meeting, meeting_id)
                other = await TranscriptService(session).append_stt_final(
                    meeting,
                    owner_id,
                    TranscriptionSegment(13000, 15000, "Người khác", "S2"),
                )
                with pytest.raises(ApiError) as mixed:
                    await service.snapshot(
                        meeting_id, owner, [segment_id, other.logical_segment_id]
                    )
                assert mixed.value.code == "VOICE_SAMPLE_MIXED_SPEAKERS"
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_short_utterances_are_combined_without_including_silence_or_other_voices(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, segment_id = await setup(tmp_path)
        try:
            async with database.session() as session:
                owner = await session.get(User, owner_id)
                meeting = await session.get(Meeting, meeting_id)
                first = (await session.scalars(select(CurrentSegment))).one()
                first.end_ms = 6000
                await session.commit()
                second = await TranscriptService(session).append_stt_final(
                    meeting,
                    owner_id,
                    TranscriptionSegment(8000, 14000, "Phát biểu tiếp", "S1"),
                )
                writer = RealtimeAudioWriter(session, storage, meeting_id, 12000, 120000)
                await writer.append(b"\x20\x00" * 16000 * 2)
                await writer.flush()
                service = MeetingVoiceLearningService(session, storage)
                meeting, segments = await service.snapshot(
                    meeting_id, owner, [second.logical_segment_id, segment_id]
                )
                sample = await service.audio(meeting, segments)
                with wave.open(io.BytesIO(sample), "rb") as audio:
                    assert audio.getnframes() == 12 * 16000
                    assert (
                        audio.readframes(audio.getnframes())
                        == b"\x10\x00" * 16000 * 10 + b"\x20\x00" * 16000 * 2
                    )
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_uploaded_audio_uses_transcript_provenance_after_manual_edit(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, segment_id = await setup(tmp_path)
        try:
            async with database.session() as session:
                meeting = await session.get(Meeting, meeting_id)
                owner = await session.get(User, owner_id)
                meeting.source_type = "upload"
                originals = []
                for name in ("correct.wav", "other.wav"):
                    media = MediaFile(
                        meeting_id=meeting_id,
                        original_filename=name,
                        mime_type="audio/wav",
                        storage_key=name,
                        duration_ms=12000,
                        size_bytes=384044,
                        status="completed",
                    )
                    session.add(media)
                    await session.flush()
                    originals.append(media)
                job = Job(
                    meeting_id=meeting_id,
                    media_file_id=originals[0].id,
                    job_type="media_processing",
                    status="completed",
                    progress=100,
                    payload={"transcript_version_id": str(meeting.current_transcript_version_id)},
                )
                session.add(job)
                await session.commit()
                await TranscriptService(session).edit_segment(
                    segment_id, owner, SegmentEditRequest(text="Đã sửa chữ")
                )
                reader = MeetingAudioReader(session, storage)
                assert (await reader._source_media(meeting)).id == originals[0].id
                job.payload = {}
                await session.commit()
                with pytest.raises(ApiError) as ambiguous:
                    await reader._source_media(meeting)
                assert ambiguous.value.code == "MEETING_AUDIO_AMBIGUOUS"
        finally:
            await database.dispose()

    asyncio.run(scenario())
