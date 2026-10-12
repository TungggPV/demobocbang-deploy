import asyncio
import wave
from array import array
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.providers.contracts import (
    BatchTranscriptionOptions,
    STTCapabilities,
    TranscriptionResult,
    TranscriptionSegment,
)
from app.services.media import MediaService
from app.services.media_worker import MediaProcessingWorker, _has_audible_pcm


def test_silent_pcm_is_rejected_before_stt(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    audible = tmp_path / "audible.wav"
    with wave.open(str(silent), "wb") as output:
        output.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        output.writeframes(array("h", [0] * 16_000).tobytes())
    with wave.open(str(audible), "wb") as output:
        output.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        output.writeframes(array("h", [0, 100, -100] * 100).tobytes())

    assert _has_audible_pcm(silent) is False
    assert _has_audible_pcm(audible) is True


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


class SuccessfulBatchSTT:
    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(
            batch=True,
            streaming=False,
            diarization=True,
            timestamps=True,
            confidence=True,
            supported_languages=frozenset({"vi", "en"}),
            multilingual_auto_detect=True,
        )

    async def transcribe_file(
        self, _source: Path, _options: BatchTranscriptionOptions
    ) -> TranscriptionResult:
        return TranscriptionResult(
            language="vi",
            segments=(
                TranscriptionSegment(
                    start_ms=0,
                    end_ms=1_000,
                    text="Nội dung vừa tải lên",
                    confidence=0.98,
                ),
            ),
        )


def test_media_upload_creates_queue_job_and_respects_acl(tmp_path: Path) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'media.sqlite3').as_posix()}"
    asyncio.run(create_schema(database_url))
    sender = CapturingEmailSender()
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=database_url,
        redis_url="redis://test.invalid:6379/0",
        jwt_secret="test-secret",
        frontend_origins="http://localhost:5173",
        media_storage_path=tmp_path / "objects",
        max_upload_bytes=1_024,
    )
    app = create_app(settings)
    app.state.email_sender = sender
    queued_job_ids: list[object] = []
    app.state.enqueue_job = lambda job_id, _settings: queued_job_ids.append(str(job_id))
    original_probe = MediaService._probe_duration

    async def valid_probe(_: MediaService, __: Path) -> int:
        return 1_200

    MediaService._probe_duration = valid_probe
    try:
        with TestClient(app) as client:
            owner = _verified_session(client, sender, "owner@example.com", "Owner")
            reader = _verified_session(client, sender, "reader@example.com", "Reader")
            owner_headers = _headers(owner)
            reader_headers = _headers(reader)
            meeting_id = _create_meeting(client, owner_headers)
            invited = client.post(
                f"/api/v1/meetings/{meeting_id}/members",
                headers=owner_headers,
                json={"email": "reader@example.com", "permission": "read"},
            )
            assert invited.status_code == 201

            uploaded = client.post(
                f"/api/v1/meetings/{meeting_id}/media",
                headers=owner_headers,
                files={"file": ("recording.mp3", b"audio", "audio/mpeg")},
            )
            assert uploaded.status_code == 202
            job = uploaded.json()
            assert queued_job_ids == [job["id"]]
            assert job["status"] == "queued"
            assert job["progress"] == 0
            assert job["media_file_id"]
            stored_files = [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
            assert len(stored_files) == 1
            assert stored_files[0].relative_to(tmp_path / "objects").as_posix().startswith(
                f"media/meetings/{meeting_id}/"
            )

            job_lookup = client.get(f"/api/v1/jobs/{job['id']}", headers=reader_headers)
            assert job_lookup.status_code == 200
            assert job_lookup.json()["id"] == job["id"]
            media = client.get(f"/api/v1/meetings/{meeting_id}/media", headers=reader_headers)
            assert media.status_code == 200
            assert media.json()[0]["duration_ms"] == 1_200
            playback = client.post(
                f"/api/v1/meetings/{meeting_id}/media/{job['media_file_id']}/playback-url",
                headers=owner_headers,
            )
            assert playback.status_code == 200
            assert playback.json()["url"].startswith("https://testserver/")
            assert (
                client.post(
                    f"/api/v1/meetings/{meeting_id}/media",
                    headers=reader_headers,
                    files={"file": ("recording.mp3", b"audio", "audio/mpeg")},
                ).status_code
                == 403
            )

            async def normalized_audio(_media: object) -> tuple[Path, Path]:
                source = tmp_path / "worker-source.wav"
                normalized = tmp_path / "worker-normalized.wav"
                source.write_bytes(b"source")
                with wave.open(str(normalized), "wb") as output:
                    output.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
                    output.writeframes(array("h", [0, 200, -200] * 1_000).tobytes())
                return source, normalized

            worker = MediaProcessingWorker(
                app.state.database,
                app.state.media_storage,
                SuccessfulBatchSTT(),
                settings,
            )
            worker._normalize = normalized_audio  # type: ignore[method-assign]
            assert client.portal.call(worker.run, UUID(job["id"])) is False

            processed_meeting = client.get(
                f"/api/v1/meetings/{meeting_id}", headers=owner_headers
            )
            assert processed_meeting.status_code == 200
            assert processed_meeting.json()["status"] == "recording"
            transcript = client.get(
                f"/api/v1/meetings/{meeting_id}/transcript", headers=owner_headers
            )
            segment_id = transcript.json()["segments"][0]["logical_segment_id"]
            assert client.patch(
                f"/api/v1/segments/{segment_id}",
                headers=reader_headers,
                json={"text": "Không được sửa"},
            ).status_code == 403
            edited = client.patch(
                f"/api/v1/segments/{segment_id}",
                headers=owner_headers,
                json={"text": "Nội dung đã sửa"},
            )
            assert edited.status_code == 200
            assert edited.json()["segment"]["text"] == "Nội dung đã sửa"
            deleted = client.delete(
                f"/api/v1/segments/{segment_id}", headers=owner_headers
            )
            assert deleted.status_code == 200

            assert (
                client.delete(
                    f"/api/v1/meetings/{meeting_id}/media/{job['media_file_id']}",
                    headers=owner_headers,
                ).status_code
                == 204
            )
            assert (
                client.get(f"/api/v1/meetings/{meeting_id}/media", headers=owner_headers).json()
                == []
            )
            assert not [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
            deleted_job = client.get(f"/api/v1/jobs/{job['id']}", headers=owner_headers)
            assert deleted_job.json()["status"] == "completed"
            assert deleted_job.json()["error"] is None
    finally:
        MediaService._probe_duration = original_probe


def test_media_rejects_unsupported_or_oversized_uploads(tmp_path: Path) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'limits.sqlite3').as_posix()}"
    asyncio.run(create_schema(database_url))
    sender = CapturingEmailSender()
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
            media_storage_path=tmp_path / "objects",
            max_upload_bytes=3,
        )
    )
    app.state.email_sender = sender

    with TestClient(app) as client:
        owner = _verified_session(client, sender, "owner@example.com", "Owner")
        headers = _headers(owner)
        realtime = client.post(
            "/api/v1/meetings",
            headers=headers,
            json={
                "title": "Realtime only",
                "source_type": "realtime",
                "language_mode": "vi",
            },
        )
        wrong_mode = client.post(
            f"/api/v1/meetings/{realtime.json()['id']}/media",
            headers=headers,
            files={"file": ("recording.wav", b"a", "audio/wav")},
        )
        assert wrong_mode.status_code == 409
        assert wrong_mode.json()["code"] == "MEETING_SOURCE_TYPE_MISMATCH"
        meeting_id = _create_meeting(client, headers)
        unsupported = client.post(
            f"/api/v1/meetings/{meeting_id}/media",
            headers=headers,
            files={"file": ("recording.txt", b"text", "text/plain")},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "MEDIA_UNSUPPORTED"
        too_large = client.post(
            f"/api/v1/meetings/{meeting_id}/media",
            headers=headers,
            files={"file": ("recording.mp3", b"toolong", "audio/mpeg")},
        )
        assert too_large.status_code == 413
        assert too_large.json()["code"] == "UPLOAD_TOO_LARGE"


def _create_meeting(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/api/v1/meetings",
        headers=headers,
        json={"title": "Media test", "source_type": "upload", "language_mode": "vi"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _verified_session(
    client: TestClient, sender: CapturingEmailSender, email: str, full_name: str
) -> dict[str, object]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery", "full_name": full_name},
    )
    assert registered.status_code == 201
    verified = client.post(
        "/api/v1/auth/verify-email-otp",
        json={"email": email, "code": sender.messages[-1][1]},
    )
    assert verified.status_code == 200
    return verified.json()


def _headers(session: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}
