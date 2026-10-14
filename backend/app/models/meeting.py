"""Meeting ownership, collaboration ACL, and share-link models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, desc
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import MeetingPermission, MeetingSourceType, MeetingStatus, domain_enum
from app.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.collaboration import Comment
    from app.models.export import Export
    from app.models.media import Job, MediaFile
    from app.models.summary import Summary
    from app.models.transcript import Speaker, Transcript, TranscriptVersion
    from app.models.translation import Translation
    from app.models.user import User


class Meeting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index(
            "ix_meetings_owner_id_updated_at_created_at",
            "owner_id",
            desc("updated_at"),
            desc("created_at"),
        ),
    )

    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[MeetingSourceType] = mapped_column(
        domain_enum(MeetingSourceType, "meeting_source_type"), nullable=False
    )
    language_mode: Mapped[str] = mapped_column(String(35), nullable=False)
    diarization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_speaker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        domain_enum(MeetingStatus, "meeting_status"),
        nullable=False,
        default=MeetingStatus.RECORDING,
        index=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_transcript_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transcript_versions.id", ondelete="SET NULL"), nullable=True
    )

    owner: Mapped[User] = relationship(back_populates="owned_meetings")
    members: Mapped[list[MeetingMember]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    media_files: Mapped[list[MediaFile]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="meeting", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )
    speakers: Mapped[list[Speaker]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    translations: Mapped[list[Translation]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    summaries: Mapped[list[Summary]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    exports: Mapped[list[Export]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    share_links: Mapped[list[ShareLink]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True
    )
    current_transcript_version: Mapped[TranscriptVersion | None] = relationship(
        foreign_keys=[current_transcript_version_id], post_update=True
    )


class MeetingMember(CreatedAtMixin, Base):
    __tablename__ = "meeting_members"
    __table_args__ = (Index("ix_meeting_members_user_id_meeting_id", "user_id", "meeting_id"),)

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    permission: Mapped[MeetingPermission] = mapped_column(
        domain_enum(MeetingPermission, "meeting_permission"), nullable=False
    )

    meeting: Mapped[Meeting] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="meeting_memberships")


class ShareLink(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "share_links"

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    permission: Mapped[MeetingPermission] = mapped_column(
        domain_enum(MeetingPermission, "meeting_permission"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    meeting: Mapped[Meeting] = relationship(back_populates="share_links")
    created_by_user: Mapped[User] = relationship(back_populates="created_share_links")
