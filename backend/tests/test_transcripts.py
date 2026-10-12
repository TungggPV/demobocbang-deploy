import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.enums import MeetingPermission, MeetingSourceType, TranscriptVersionSource
from app.models.meeting import Meeting, MeetingMember
from app.models.transcript import (
    CurrentSegment,
    SegmentVersion,
    Speaker,
    Transcript,
    TranscriptEditEvent,
    TranscriptVersion,
)
from app.providers.contracts import TranscriptionSegment
from app.services.transcripts import TranscriptService


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


def test_get_current_transcript_and_history_are_ordered_and_immutable(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "owner@example.com")
        seeded = _seed(url, UUID(owner["user"]["id"]))
        headers = _headers(owner)
        current = client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers)
        assert current.status_code == 200
        assert [item["text"] for item in current.json()["segments"]] == ["First segment", "Second segment"]
        assert current.json()["segments"][0]["id"] == seeded["first"]
        first_page = client.get(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript?limit=1",
            headers=headers,
        )
        paged = client.get(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript?limit=1&cursor={first_page.json()['next_cursor']}",
            headers=headers,
        )
        assert [item["text"] for item in paged.json()["segments"]] == ["Second segment"]
        edited = client.patch(f"/api/v1/segments/{seeded['first']}", headers=headers, json={"text": "Edited"})
        assert edited.status_code == 200
        history = client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript/versions", headers=headers)
        assert [item["version_no"] for item in history.json()] == [2, 1]
        assert client.get(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript/versions?limit=1",
            headers=headers,
        ).json()[0]["version_no"] == 2
        old = client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript/versions/{seeded['version_id']}", headers=headers)
        assert old.json()["segments"][0]["text"] == "First segment"


def test_edit_split_merge_delete_and_version_creation(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "editor@example.com")
        seeded = _seed(url, UUID(owner["user"]["id"]))
        headers = _headers(owner)
        edited = client.patch(f"/api/v1/segments/{seeded['first']}", headers=headers, json={"start_ms": 100, "text": "Alpha beta"})
        assert edited.status_code == 200 and edited.json()["version_no"] == 2
        split = client.post(f"/api/v1/segments/{seeded['first']}/split", headers=headers, json={"split_at_ms": 500, "first_text": "Alpha", "second_text": "beta"})
        assert split.status_code == 200 and split.json()["version_no"] == 3
        current = client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers).json()
        assert [(item["start_ms"], item["end_ms"]) for item in current["segments"]] == [(100, 500), (500, 1000), (1000, 2000)]
        second_part = current["segments"][1]["logical_segment_id"]
        merged = client.post("/api/v1/segments/merge", headers=headers, json={"first_segment_id": seeded["first"], "second_segment_id": second_part})
        assert merged.status_code == 200 and merged.json()["version_no"] == 4
        deleted = client.delete(f"/api/v1/segments/{seeded['second']}", headers=headers)
        assert deleted.status_code == 200 and deleted.json()["version_no"] == 5
        assert len(client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers).json()["segments"]) == 1


def test_text_edit_split_and_multi_merge_work_with_zero_duration_segment(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "text-tools@example.com")
        seeded = _seed(url, UUID(owner["user"]["id"]))

        async def make_zero_duration() -> None:
            engine = create_async_engine(url)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                segment = await session.scalar(
                    select(CurrentSegment).where(
                        CurrentSegment.logical_segment_id == UUID(seeded["first"])
                    )
                )
                assert segment is not None
                segment.end_ms = segment.start_ms
                await session.commit()
            await engine.dispose()

        asyncio.run(make_zero_duration())
        headers = _headers(owner)
        edited = client.patch(
            f"/api/v1/segments/{seeded['first']}",
            headers=headers,
            json={"text": "Một hai"},
        )
        assert edited.status_code == 200
        split = client.post(
            f"/api/v1/segments/{seeded['first']}/split",
            headers=headers,
            json={"split_at_ms": 1, "first_text": "Một", "second_text": "hai"},
        )
        assert split.status_code == 200
        segments = client.get(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers
        ).json()["segments"]
        merged = client.post(
            "/api/v1/segments/merge",
            headers=headers,
            json={"segment_ids": [item["logical_segment_id"] for item in segments]},
        )
        assert merged.status_code == 200
        current = client.get(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers
        ).json()["segments"]
        assert len(current) == 1
        assert current[0]["text"] == "Một hai Second segment"


def test_speaker_assignment_and_edit_permissions(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "speaker-owner@example.com")
        commenter = _session(client, sender, "commenter@example.com")
        seeded = _seed(url, UUID(owner["user"]["id"]), UUID(commenter["user"]["id"]))
        owner_headers, commenter_headers = _headers(owner), _headers(commenter)
        assert client.get(f"/api/v1/meetings/{seeded['meeting_id']}/speakers", headers=commenter_headers).status_code == 200
        assert client.patch(f"/api/v1/segments/{seeded['first']}", headers=commenter_headers, json={"text": "No"}).status_code == 403
        speaker = client.post(f"/api/v1/meetings/{seeded['meeting_id']}/speakers", headers=owner_headers, json={"name": "Alice", "role": "Host"})
        assert speaker.status_code == 201
        speaker_id = speaker.json()["id"]
        assigned = client.patch(f"/api/v1/segments/{seeded['first']}/speaker", headers=owner_headers, json={"speaker_id": speaker_id})
        assert assigned.status_code == 200
        assert assigned.json()["segment"]["speaker_id"] == speaker_id
        reassigned = client.patch(
            f"/api/v1/segments/{seeded['first']}/speaker",
            headers=owner_headers,
            json={"speaker_id": seeded["speaker_id"]},
        )
        assert reassigned.status_code == 200
        assert reassigned.json()["segment"]["speaker_id"] == seeded["speaker_id"]
        updated = client.patch(f"/api/v1/speakers/{speaker_id}", headers=owner_headers, json={"name": "Alice Nguyen"})
        assert updated.status_code == 200 and updated.json()["name"] == "Alice Nguyen"
        assert client.patch(f"/api/v1/speakers/{speaker_id}", headers=commenter_headers, json={"name": "No"}).status_code == 403


def test_rejects_invalid_timestamps_and_cross_meeting_speaker(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "validation@example.com")
        one = _seed(url, UUID(owner["user"]["id"]))
        two = _seed(url, UUID(owner["user"]["id"]))
        headers = _headers(owner)
        before = client.get(f"/api/v1/meetings/{one['meeting_id']}/transcript", headers=headers).json()["version_id"]
        invalid = client.patch(f"/api/v1/segments/{one['first']}", headers=headers, json={"start_ms": 1200, "end_ms": 1100})
        assert invalid.status_code == 422
        assert client.get(f"/api/v1/meetings/{one['meeting_id']}/transcript", headers=headers).json()["version_id"] == before
        overlap = client.patch(f"/api/v1/segments/{one['second']}", headers=headers, json={"start_ms": 900})
        assert overlap.status_code == 422
        wrong_speaker = client.patch(f"/api/v1/segments/{one['first']}/speaker", headers=headers, json={"speaker_id": two["speaker_id"]})
        assert wrong_speaker.status_code == 404


def test_replace_stt_batch_replaces_existing_projection(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "batch-replace@example.com")
        seeded = _seed(url, UUID(owner["user"]["id"]))

        async def replace() -> None:
            engine = create_async_engine(url)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                meeting = await session.get(Meeting, UUID(seeded["meeting_id"]))
                assert meeting is not None
                await TranscriptService(session).replace_stt_batch(
                    meeting,
                    UUID(owner["user"]["id"]),
                    (
                        TranscriptionSegment(0, 700, "Xin chào"),
                        TranscriptionSegment(700, 1400, "Việt Nam"),
                    ),
                )
                await session.commit()
                transcript = await session.scalar(
                    select(Transcript).where(Transcript.meeting_id == meeting.id)
                )
                assert transcript is not None
                current = list(await session.scalars(
                    select(CurrentSegment)
                    .where(CurrentSegment.transcript_id == transcript.id)
                    .order_by(CurrentSegment.position)
                ))
                assert [item.text for item in current] == ["Xin chào.", "Việt Nam."]
                assert [item.position for item in current] == [Decimal(1024), Decimal(2048)]
            await engine.dispose()

        asyncio.run(replace())


def test_projection_events_checkpoints_rollback_and_legacy_fallback(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "projection@example.com")
        headers = _headers(owner)
        seeded = _seed(url, UUID(owner["user"]["id"]))
        for index in range(25):
            response = client.patch(
                f"/api/v1/segments/{seeded['first']}", headers=headers, json={"text": f"Edit {index}"}
            )
            assert response.status_code == 200
        checkpoint_version_id = response.json()["version_id"]
        current = client.get(f"/api/v1/meetings/{seeded['meeting_id']}/transcript", headers=headers).json()
        rollback = client.post(
            f"/api/v1/meetings/{seeded['meeting_id']}/transcript/rollback",
            headers=headers,
            json={"target_version_id": seeded["version_id"]},
        )
        assert rollback.status_code == 200

        async def verify() -> None:
            engine = create_async_engine(url)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                events = list(await session.scalars(select(TranscriptEditEvent)))
                assert all(event.actor_id is not None for event in events)
                checkpoint = await session.get(TranscriptVersion, UUID(checkpoint_version_id))
                assert checkpoint is not None and checkpoint.checkpointed_at is not None
                version = await session.get(TranscriptVersion, UUID(rollback.json()["version_id"]))
                assert version is not None and version.checkpointed_at is not None
                rollback_event = next(event for event in events if event.version_id == version.id)
                assert rollback_event.event_type == "rollback"
                assert rollback_event.payload["target_version_id"] == seeded["version_id"]
            await engine.dispose()

        asyncio.run(verify())
        assert current["segments"][0]["text"] == "Edit 24"

        legacy = _seed(url, UUID(owner["user"]["id"]), projection=False)
        assert client.get(f"/api/v1/meetings/{legacy['meeting_id']}/transcript", headers=headers).status_code == 200
        assert client.patch(f"/api/v1/segments/{legacy['first']}", headers=headers, json={"text": "No"}).status_code == 409


def _app(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'transcript.sqlite3').as_posix()}"

    async def create_schema() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create_schema())
    app = create_app(Settings(_env_file=None, database_url=url, jwt_secret="test-secret", frontend_origins="http://localhost:5173"))
    sender = CapturingEmailSender()
    app.state.email_sender = sender
    return app, sender, url


def _session(client: TestClient, sender: CapturingEmailSender, email: str) -> dict:
    assert client.post("/api/v1/auth/register", json={"email": email, "password": "correct horse battery", "full_name": email}).status_code == 201
    response = client.post("/api/v1/auth/verify-email-otp", json={"email": email, "code": sender.messages[-1][1]})
    assert response.status_code == 200
    return response.json()


def _headers(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _seed(url: str, owner_id: UUID, commenter_id: UUID | None = None, projection: bool = True) -> dict[str, str]:
    async def seed() -> dict[str, str]:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            meeting = Meeting(owner_id=owner_id, title="Transcript", source_type=MeetingSourceType.REALTIME, language_mode="en")
            transcript = Transcript(meeting=meeting)
            version = TranscriptVersion(transcript=transcript, version_no=1, source=TranscriptVersionSource.STT, created_by=owner_id)
            speaker = Speaker(meeting=meeting, name="Provider speaker", provider_speaker_id="speaker_1")
            first = SegmentVersion(transcript_version=version, speaker=speaker, sequence=1, start_ms=0, end_ms=1000, text="First segment", original_stt_text="First segment")
            second = SegmentVersion(transcript_version=version, speaker=speaker, sequence=2, start_ms=1000, end_ms=2000, text="Second segment", original_stt_text="Second segment")
            if commenter_id:
                session.add(MeetingMember(meeting=meeting, user_id=commenter_id, permission=MeetingPermission.COMMENT))
            session.add_all((meeting, transcript, version, speaker, first, second))
            await session.flush()
            meeting.current_transcript_version_id = version.id
            if projection:
                transcript.current_projection_initialized_at = datetime.now(UTC)
                version.checkpointed_at = datetime.now(UTC)
                session.add_all((
                    CurrentSegment(transcript_id=transcript.id, logical_segment_id=first.logical_segment_id, speaker_id=speaker.id, position=Decimal(1024), start_ms=0, end_ms=1000, text="First segment", original_stt_text="First segment"),
                    CurrentSegment(transcript_id=transcript.id, logical_segment_id=second.logical_segment_id, speaker_id=speaker.id, position=Decimal(2048), start_ms=1000, end_ms=2000, text="Second segment", original_stt_text="Second segment"),
                ))
            await session.commit()
            result = {"meeting_id": str(meeting.id), "version_id": str(version.id), "speaker_id": str(speaker.id), "first": str(first.logical_segment_id), "second": str(second.logical_segment_id)}
        await engine.dispose()
        return result

    return asyncio.run(seed())
