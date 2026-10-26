import asyncio

from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import Database
from app.models.enums import (
    MeetingPermission,
    MeetingSourceType,
    SummaryTemplateType,
    TranscriptVersionSource,
)
from app.models.meeting import Meeting, MeetingMember
from app.models.summary import Summary, SummaryVersion
from app.models.transcript import SegmentVersion, Speaker, Transcript, TranscriptVersion
from app.models.user import User


def test_async_session_persists_versioned_transcript_and_summary() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(connection: object, _: object) -> None:
            connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[union-attr]

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            owner = User(email="owner@example.com", full_name="Owner")
            collaborator = User(email="member@example.com", full_name="Member")
            meeting = Meeting(
                owner=owner,
                title="Architecture review",
                source_type=MeetingSourceType.UPLOAD,
                language_mode="vi",
                diarization_enabled=True,
            )
            membership = MeetingMember(
                meeting=meeting, user=collaborator, permission=MeetingPermission.EDIT
            )
            transcript = Transcript(meeting=meeting)
            version = TranscriptVersion(
                transcript=transcript,
                version_no=1,
                source=TranscriptVersionSource.STT,
                created_by_user=owner,
            )
            speaker = Speaker(meeting=meeting, name="Speaker 1")
            segment = SegmentVersion(
                transcript_version=version,
                sequence=0,
                start_ms=0,
                end_ms=1200,
                text="Xin chào",
                speaker=speaker,
            )
            summary = Summary(
                meeting=meeting,
                transcript_version=version,
                template_type=SummaryTemplateType.GENERAL,
                output_language="vi",
            )
            summary_version = SummaryVersion(
                summary=summary,
                version_no=1,
                prompt="Tóm tắt nội dung chính.",
                content={"overview": "Greeting"},
                provider="test-provider",
                model="test-model",
                created_by_user=owner,
            )
            meeting.current_transcript_version = version
            session.add_all([meeting, membership, segment, summary_version])
            await session.commit()

            stored_meeting = await session.scalar(select(Meeting).where(Meeting.id == meeting.id))
            assert stored_meeting is not None
            assert stored_meeting.current_transcript_version_id == version.id
            assert membership.permission == MeetingPermission.EDIT
            assert segment.logical_segment_id is not None
            assert summary_version.summary_id == summary.id

            session.add(User(email="owner@example.com", full_name="Duplicate"))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
            else:
                raise AssertionError("users.email must be unique")

        await engine.dispose()

    asyncio.run(scenario())


def test_database_wrapper_opens_an_async_session() -> None:
    async def scenario() -> None:
        database = Database("sqlite+aiosqlite:///:memory:")
        assert database.is_configured
        assert await database.ping()
        async with database.session() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        await database.dispose()

    asyncio.run(scenario())
