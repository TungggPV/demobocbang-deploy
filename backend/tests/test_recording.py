import asyncio
import io
import wave
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.datastructures import State
from test_transcripts import _app, _headers, _session
from test_voice_learning import setup

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.media import Job, MediaFile, MeetingAudioChunk
from app.models.meeting import Meeting, MeetingMember
from app.models.user import User
from app.providers.storage import StorageError
from app.services.media import MediaService
from app.services.meeting_audio import RealtimeAudioWriter
from app.services.recording import RecordingService, RecordingWorker
from app.services.transcript_polish import complete_with_polish


def test_completion_enqueues_once_without_ai_and_produces_exact_persistent_audio(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        settings = Settings(_env_file=None)
        queued = []
        state = State(
            {"settings": settings, "enqueue_job": lambda job_id, _: queued.append(job_id)}
        )
        try:
            async with database.session() as session:
                owner = await session.get(User, owner_id)
                writer = RealtimeAudioWriter(session, storage, meeting_id, 12000, 120000)
                await writer.append(b"\x20\x00" * 16000 * 3)
                await writer.flush()
                await complete_with_polish(session, meeting_id, owner, state)
                await complete_with_polish(session, meeting_id, owner, state)
                assert len(queued) == 1
                pending = await RecordingService(session).status(meeting_id, owner)
                assert pending.status == "pending" and not pending.can_prepare
                assert (await RecordingService(session).prepare(meeting_id, owner)).id == queued[0]
            worker = RecordingWorker(database, storage, settings)
            assert not await worker.run(queued[0])
            assert not await worker.run(queued[0])
            async with database.session() as session:
                ready = await RecordingService(session).status(
                    meeting_id, await session.get(User, owner_id)
                )
                assert ready.status == "ready" and not ready.can_prepare
                assert ready.media.duration_ms == 15000
                media = await session.get(MediaFile, ready.media.id)
                content = await storage.get_bytes(media.storage_key)
                with wave.open(io.BytesIO(content), "rb") as audio:
                    assert audio.getnframes() == 15000 * 16
                    assert audio.readframes(12000 * 16) == b"\x10\x00" * 16000 * 12
                    assert audio.readframes(3000 * 16) == b"\x20\x00" * 16000 * 3
                assert len(list(await session.scalars(select(MediaFile)))) == 1
                assert (await session.get(Meeting, meeting_id)).status == "completed"
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_http_recording_status_prepare_range_download_and_acl(tmp_path):
    app, sender, _ = _app(tmp_path)
    queued = []
    app.state.enqueue_job = lambda job_id, _: queued.append(job_id)
    with TestClient(app) as client:
        owner = _session(client, sender, "recording-owner@example.com")
        outsider = _session(client, sender, "recording-other@example.com")
        headers = _headers(owner)
        meeting_id = client.post(
            "/api/v1/meetings",
            headers=headers,
            json={
                "title": "Recording",
                "source_type": "realtime",
                "language_mode": "vi",
                "diarization_enabled": False,
            },
        ).json()["id"]
        root = f"/api/v1/meetings/{meeting_id}"
        assert client.get(root + "/recording", headers=headers).json()["status"] == "not_completed"
        assert client.get(root + "/recording", headers=_headers(outsider)).status_code == 403
        assert client.post(root + "/recording", headers=headers).status_code == 409

        async def seed():
            async with app.state.database.session() as session:
                writer = RealtimeAudioWriter(
                    session, app.state.media_storage, UUID(meeting_id), 0, 120000
                )
                await writer.append(b"\x12\x00" * 16000 * 40)
                await writer.flush()

        asyncio.run(seed())
        assert client.post(root + "/complete", headers=headers).status_code == 200
        pending = client.get(root + "/recording", headers=headers).json()
        assert pending["status"] == "pending" and len(queued) == 1
        assert client.post(root + "/recording", headers=headers).json()["job"]["id"] == str(
            queued[0]
        )
        asyncio.run(
            RecordingWorker(app.state.database, app.state.media_storage, app.state.settings).run(
                queued[0]
            )
        )
        ready = client.get(root + "/recording", headers=headers).json()
        media_id = ready["media"]["id"]
        base = root + f"/media/{media_id}"
        ticket = client.post(base + "/playback-url", headers=headers).json()["url"]
        partial = client.get(ticket, headers={"Range": "bytes=0-43"})
        assert partial.status_code == 206 and partial.content[:4] == b"RIFF"
        assert partial.headers["Content-Length"] == "44"
        downloaded = client.get(base + "/download", headers=headers)
        assert downloaded.status_code == 200 and downloaded.content[:4] == b"RIFF"
        assert len(downloaded.content) == 40 * 32000 + 44
        assert "attachment" in downloaded.headers["Content-Disposition"]
        assert client.get(base + "/download", headers=_headers(outsider)).status_code == 403
        assert client.delete(base, headers=headers).status_code == 204
        assert client.get(ticket).status_code == 404
        assert client.get(root + "/recording", headers=headers).json()["status"] == "deleted"
        assert client.post(root + "/recording", headers=headers).status_code == 409


@pytest.mark.parametrize(
    "damage,code",
    [
        ("overlap", "RECORDING_OVERLAP"),
        ("gap", "RECORDING_INCOMPLETE"),
        ("missing", "RECORDING_UNAVAILABLE"),
        ("corrupt", "RECORDING_INVALID"),
    ],
)
def test_bad_chunks_never_publish_partial_audio_or_fail_the_meeting(tmp_path, damage, code):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        try:
            async with database.session() as session:
                chunks = list(
                    await session.scalars(
                        select(MeetingAudioChunk).order_by(MeetingAudioChunk.start_ms)
                    )
                )
                if damage == "gap":
                    await storage.delete(chunks[1].storage_key)
                    await session.delete(chunks[1])
                elif damage == "overlap":
                    chunks[1].start_ms -= 100
                elif damage == "missing":
                    await storage.delete(chunks[1].storage_key)
                else:
                    await storage.put_bytes(b"broken", chunks[1].storage_key)
                meeting = await session.get(Meeting, meeting_id)
                meeting.status = "completed"
                await session.commit()
                job = await RecordingService(session).prepare(
                    meeting_id, await session.get(User, owner_id)
                )
            assert not await RecordingWorker(database, storage, Settings(_env_file=None)).run(
                job.id
            )
            async with database.session() as session:
                job = await session.get(Job, job.id)
                assert job.status == "failed" and job.error_code == code
                assert not list(await session.scalars(select(MediaFile)))
                assert (await session.get(Meeting, meeting_id)).status == "completed"
                status = await RecordingService(session).status(
                    meeting_id, await session.get(User, owner_id)
                )
                assert status.status == "failed" and status.can_prepare
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_worker_dispatch_does_not_initialize_ai_or_stt(tmp_path, monkeypatch):
    from app.workers import tasks

    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        try:
            async with database.session() as session:
                meeting = await session.get(Meeting, meeting_id)
                meeting.status = "completed"
                await session.commit()
                job = await RecordingService(session).prepare(
                    meeting_id, await session.get(User, owner_id)
                )
            database_url = "sqlite+aiosqlite:///" + (tmp_path / "voice-learning.sqlite").as_posix()
            settings = Settings(
                _env_file=None,
                database_url=database_url,
                media_storage_path=tmp_path / "audio",
            )
            monkeypatch.setattr(tasks, "get_settings", lambda: settings)

            def forbidden(*_):
                raise AssertionError("Playback assembly must not initialize AI/STT")

            for name in ("build_stt_provider", "build_llm_provider", "build_translation_provider"):
                monkeypatch.setattr(tasks, name, forbidden)
            assert not await tasks._execute(job.id)
            async with database.session() as session:
                assert (
                    await RecordingService(session).status(
                        meeting_id, await session.get(User, owner_id)
                    )
                ).status == "ready"
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_interconnection_gap_preserves_timestamps_and_is_reported(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        try:
            async with database.session() as session:
                writer = RealtimeAudioWriter(session, storage, meeting_id, 12500, 120000)
                await writer.append(b"\x30\x00" * 16000)
                await writer.flush()
                meeting = await session.get(Meeting, meeting_id)
                meeting.status = "completed"
                await session.commit()
                job = await RecordingService(session).prepare(
                    meeting_id, await session.get(User, owner_id)
                )
            await RecordingWorker(database, storage, Settings(_env_file=None)).run(job.id)
            async with database.session() as session:
                status = await RecordingService(session).status(
                    meeting_id, await session.get(User, owner_id)
                )
                assert status.status == "ready" and "0.5" in status.warning
                media = await session.get(MediaFile, status.media.id)
                with wave.open(io.BytesIO(await storage.get_bytes(media.storage_key))) as audio:
                    audio.setpos(12000 * 16)
                    assert audio.readframes(500 * 16) == bytes(500 * 32)
                    assert audio.readframes(1000 * 16) == b"\x30\x00" * 16000
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_old_meeting_acl_no_audio_and_deleted_recording_are_honest(tmp_path):
    async def scenario():
        database, storage, owner_id, outsider_id, meeting_id, _ = await setup(tmp_path)
        settings = Settings(_env_file=None)
        try:
            async with database.session() as session:
                owner, outsider = (
                    await session.get(User, owner_id),
                    await session.get(User, outsider_id),
                )
                service = RecordingService(session)
                assert (await service.status(meeting_id, owner)).status == "not_completed"
                with pytest.raises(ApiError):
                    await service.prepare(meeting_id, owner)
                meeting = await session.get(Meeting, meeting_id)
                meeting.status = "completed"
                await session.commit()
                with pytest.raises(ApiError):
                    await service.status(meeting_id, outsider)
                session.add(
                    MeetingMember(meeting_id=meeting_id, user_id=outsider_id, permission="read")
                )
                await session.commit()
                assert not (await service.status(meeting_id, outsider)).can_prepare
                with pytest.raises(ApiError):
                    await service.prepare(meeting_id, outsider)
                job = await service.prepare(meeting_id, owner)
            await RecordingWorker(database, storage, settings).run(job.id)
            async with database.session() as session:
                owner = await session.get(User, owner_id)
                status = await RecordingService(session).status(meeting_id, owner)
                media = await session.get(MediaFile, status.media.id)
                assert (
                    await MediaService(session, settings, storage).read_media_range(
                        meeting_id, media.id, owner, 0, 43
                    )
                )[0][:4] == b"RIFF"
                await MediaService(session, settings, storage).delete_media(
                    meeting_id, media.id, owner
                )
                assert not list(await session.scalars(select(MeetingAudioChunk)))
                assert (
                    await RecordingService(session).status(meeting_id, owner)
                ).status == "deleted"
                with pytest.raises(ApiError, match="đã được xóa"):
                    await RecordingService(session).prepare(meeting_id, owner)
                old = Meeting(
                    id=uuid4(),
                    owner_id=owner_id,
                    title="Old",
                    source_type="realtime",
                    language_mode="vi",
                    status="completed",
                )
                session.add(old)
                await session.commit()
                unavailable = await RecordingService(session).status(old.id, owner)
                assert unavailable.status == "unavailable" and not unavailable.can_prepare
                with pytest.raises(ApiError, match="Không còn âm thanh"):
                    await RecordingService(session).prepare(old.id, owner)
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_delivery_failure_keeps_completion_and_retry_publishes_once(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        settings = Settings(_env_file=None)

        def offline(*_):
            raise RuntimeError("queue offline")

        try:
            async with database.session() as session:
                owner = await session.get(User, owner_id)
                await complete_with_polish(
                    session,
                    meeting_id,
                    owner,
                    State({"enqueue_job": offline, "settings": settings}),
                )
                status = await RecordingService(session).status(meeting_id, owner)
                assert status.status == "pending"
                job_id = status.job.id
                assert (await session.get(Meeting, meeting_id)).status == "completed"
            put_file = storage.put_file

            async def fail_publish(*_):
                raise StorageError("offline")

            storage.put_file = fail_publish
            worker = RecordingWorker(database, storage, settings)
            assert await worker.run(job_id)
            async with database.session() as session:
                assert (await session.get(Job, job_id)).status == "retrying"
                assert not list(await session.scalars(select(MediaFile)))
            storage.put_file = put_file
            assert not await worker.run(job_id)
            async with database.session() as session:
                assert (
                    await RecordingService(session).status(
                        meeting_id, await session.get(User, owner_id)
                    )
                ).status == "ready"
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_changed_source_fails_then_explicit_retry_uses_new_snapshot(tmp_path):
    async def scenario():
        database, storage, owner_id, _, meeting_id, _ = await setup(tmp_path)
        try:
            async with database.session() as session:
                meeting = await session.get(Meeting, meeting_id)
                meeting.status = "completed"
                await session.commit()
                owner = await session.get(User, owner_id)
                first = await RecordingService(session).prepare(meeting_id, owner)
                writer = RealtimeAudioWriter(session, storage, meeting_id, 12000, 120000)
                await writer.append(b"\x40\x00" * 16000)
                await writer.flush()
            worker = RecordingWorker(database, storage, Settings(_env_file=None))
            assert not await worker.run(first.id)
            async with database.session() as session:
                assert (await session.get(Job, first.id)).error_code == "RECORDING_CHANGED"
                next_job = await RecordingService(session).prepare(
                    meeting_id, await session.get(User, owner_id)
                )
                assert next_job.id != first.id
            assert not await worker.run(next_job.id)
            async with database.session() as session:
                status = await RecordingService(session).status(
                    meeting_id, await session.get(User, owner_id)
                )
                assert status.status == "ready" and status.media.duration_ms == 13000
        finally:
            await database.dispose()

    asyncio.run(scenario())
