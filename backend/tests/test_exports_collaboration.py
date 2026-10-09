import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.enums import MeetingPermission, MeetingSourceType, TranscriptVersionSource
from app.models.meeting import Meeting, MeetingMember
from app.models.transcript import SegmentVersion, Speaker, Transcript, TranscriptVersion
from app.models.translation import Translation, TranslationSegment
from app.providers.email import EmailAttachment
from app.services.ai import AIJobDispatcher


class Sender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.invitations: list[tuple[str, list[EmailAttachment]]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))

    async def send_invitation(
        self,
        recipient: str,
        meeting_title: str,
        permission: str,
        url: str,
        attachments: Sequence[EmailAttachment] = (),
    ) -> None:
        self.invitations.append((recipient, list(attachments)))


def test_exports_all_formats_and_collaboration_permissions(tmp_path: Path) -> None:
    async def create_schema(url: str) -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    url = f"sqlite+aiosqlite:///{(tmp_path / 'task11.sqlite3').as_posix()}"
    asyncio.run(create_schema(url))
    sender = Sender()
    app = create_app(
        Settings(
            _env_file=None,
            database_url=url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
            media_storage_path=tmp_path / "objects",
        )
    )
    app.state.email_sender = sender

    async def presigned_url(*_args: object, **_kwargs: object) -> str:
        return "https://storage.example.invalid/export"

    # Production S3 storage can create presigned URLs, but browser downloads
    # must still be proxied to avoid depending on bucket CORS configuration.
    app.state.media_storage.presigned_get_url = presigned_url

    def enqueue(job_id: UUID, _settings: Settings) -> None:
        def execute() -> None:
            async def run_until_terminal() -> None:
                dispatcher = AIJobDispatcher(
                    app.state.database,
                    app.state.settings,
                    app.state.translation_provider,
                    app.state.llm_provider,
                    app.state.media_storage,
                )
                while await dispatcher.run(UUID(str(job_id))):
                    pass

            asyncio.run(run_until_terminal())

        Thread(target=execute, daemon=True).start()

    # Keep this integration test independent from REDIS_URL inherited from the
    # host/container. A live Dramatiq worker cannot read this test's SQLite DB.
    app.state.enqueue_job = enqueue
    with TestClient(app) as client:
        owner = _session(client, sender, "owner@example.com")
        commenter = _session(client, sender, "comment@example.com")
        reader = _session(client, sender, "reader@example.com")
        meeting_id = _seed(
            url,
            UUID(owner["user"]["id"]),
            UUID(commenter["user"]["id"]),
            UUID(reader["user"]["id"]),
        )
        owner_headers, commenter_headers, reader_headers = (
            _headers(owner),
            _headers(commenter),
            _headers(reader),
        )
        for format in ("txt", "json", "srt", "vtt", "pdf", "docx"):
            response = client.post(
                f"/api/v1/meetings/{meeting_id}/exports",
                headers=owner_headers,
                json={"format": format},
            )
            assert response.status_code == 202
            completed = _wait_export(client, owner_headers, response.json()["id"])
            assert completed["status"] == "completed"
            download = client.get(
                f"/api/v1/exports/{completed['id']}/download",
                headers=reader_headers,
                follow_redirects=False,
            )
            assert download.status_code == 200
            if format in {"txt", "json", "srt", "vtt"}:
                assert "Xin chào Việt Nam" in download.content.decode("utf-8")
        translated_export = client.post(
            f"/api/v1/meetings/{meeting_id}/exports",
            headers=owner_headers,
            json={
                "format": "txt",
                "options": {"content_mode": "translation"},
            },
        )
        assert translated_export.status_code == 202
        translated = _wait_export(client, owner_headers, translated_export.json()["id"])
        translated_file = client.get(
            f"/api/v1/exports/{translated['id']}/download", headers=owner_headers
        )
        translated_text = translated_file.content.decode("utf-8")
        assert "BẢN DỊCH CUỘC HỌP" in translated_text
        assert "Hello Vietnam" in translated_text
        assert "Xin chào Việt Nam" not in translated_text
        assert (
            client.post(
                f"/api/v1/meetings/{meeting_id}/exports",
                headers=_headers(_session(client, sender, "outside@example.com")),
                json={"format": "txt"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/meetings/{meeting_id}/comments",
                headers=reader_headers,
                json={"body": "no"},
            ).status_code
            == 403
        )
        comment = client.post(
            f"/api/v1/meetings/{meeting_id}/comments",
            headers=commenter_headers,
            json={"body": "Có ghi chú"},
        )
        assert comment.status_code == 201
        emailed = client.post(
            f"/api/v1/meetings/{meeting_id}/members",
            headers=owner_headers,
            json={
                "email": "new-reader@example.com",
                "permission": "read",
                "attachments": ["transcript"],
            },
        )
        assert emailed.status_code == 201
        assert emailed.json()["status"] == "invitation_sent"
        assert sender.invitations[-1][0] == "new-reader@example.com"
        assert [item.filename for item in sender.invitations[-1][1]] == [
            "Họp-Việt-Nam-noi-dung.txt"
        ]
        assert "Xin chào Việt Nam" in sender.invitations[-1][1][0].content.decode("utf-8")
        assert len(client.get(
            f"/api/v1/meetings/{meeting_id}/comments?limit=1&offset=0",
            headers=commenter_headers,
        ).json()) == 1
        segment_id = client.get(
            f"/api/v1/meetings/{meeting_id}/transcript", headers=owner_headers
        ).json()["segments"][0]["logical_segment_id"]
        assert (
            client.patch(
                f"/api/v1/segments/{segment_id}",
                headers=commenter_headers,
                json={"text": "no"},
            ).status_code
            == 403
        )
        share = client.post(
            f"/api/v1/meetings/{meeting_id}/shares",
            headers=owner_headers,
            json={"permission": "comment"},
        ).json()
        assert client.get(f"/api/v1/shares/{share['token']}").json()["permission"] == "comment"
        assert (
            client.delete(
                f"/api/v1/meetings/shares/{share['id']}", headers=owner_headers
            ).status_code
            == 204
        )
        assert client.get(f"/api/v1/shares/{share['token']}").status_code == 404


def _wait_export(client: TestClient, headers: dict[str, str], export_id: str) -> dict[str, object]:
    for _ in range(40):
        value = client.get(f"/api/v1/exports/{export_id}", headers=headers).json()
        if value["status"] in {"completed", "failed"}:
            assert value["status"] == "completed", value
            return value
        time.sleep(0.02)
    raise AssertionError("export job did not complete")


def _seed(url: str, owner_id: UUID, commenter_id: UUID, reader_id: UUID) -> str:
    from app.db.session import Database

    async def seed() -> str:
        database = Database(url)
        async with database.session() as session:
            meeting = Meeting(
                owner_id=owner_id,
                title="Họp Việt Nam",
                source_type=MeetingSourceType.UPLOAD,
                language_mode="vi",
            )
            session.add(meeting)
            await session.flush()
            session.add_all(
                [
                    MeetingMember(
                        meeting_id=meeting.id,
                        user_id=commenter_id,
                        permission=MeetingPermission.COMMENT,
                    ),
                    MeetingMember(
                        meeting_id=meeting.id, user_id=reader_id, permission=MeetingPermission.READ
                    ),
                ]
            )
            transcript = Transcript(meeting_id=meeting.id)
            session.add(transcript)
            await session.flush()
            version = TranscriptVersion(
                transcript_id=transcript.id, version_no=1, source=TranscriptVersionSource.STT
            )
            session.add(version)
            await session.flush()
            speaker = Speaker(meeting_id=meeting.id, name="Người nói 1")
            session.add(speaker)
            await session.flush()
            logical_segment_id = uuid4()
            session.add(
                SegmentVersion(
                    transcript_version_id=version.id,
                    logical_segment_id=logical_segment_id,
                    speaker_id=speaker.id,
                    sequence=1,
                    start_ms=0,
                    end_ms=1250,
                    text="Xin chào Việt Nam",
                )
            )
            translation = Translation(
                meeting_id=meeting.id,
                transcript_version_id=version.id,
                source_language="vi",
                target_language="en",
            )
            session.add(translation)
            await session.flush()
            session.add(
                TranslationSegment(
                    translation_id=translation.id,
                    logical_segment_id=logical_segment_id,
                    sequence=1,
                    text="Hello Vietnam",
                )
            )
            meeting.current_transcript_version_id = version.id
            await session.commit()
            return str(meeting.id)
        await database.dispose()

    return asyncio.run(seed())


def _session(client: TestClient, sender: Sender, email: str) -> dict[str, object]:
    assert (
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "correct horse battery", "full_name": email},
        ).status_code
        == 201
    )
    return client.post(
        "/api/v1/auth/verify-email-otp", json={"email": email, "code": sender.messages[-1][1]}
    ).json()


def _headers(session: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}
