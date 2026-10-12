"""User identity and email-verification persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, desc, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import EmailOtpPurpose, domain_enum
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.collaboration import Comment
    from app.models.meeting import Meeting, MeetingMember, ShareLink
    from app.models.transcript import TranscriptVersion


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    google_subject: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    email_otps: Mapped[list[EmailOtp]] = relationship(back_populates="user")
    owned_meetings: Mapped[list[Meeting]] = relationship(back_populates="owner")
    meeting_memberships: Mapped[list[MeetingMember]] = relationship(back_populates="user")
    comments: Mapped[list[Comment]] = relationship(back_populates="author")
    created_transcript_versions: Mapped[list[TranscriptVersion]] = relationship(
        back_populates="created_by_user", foreign_keys="TranscriptVersion.created_by"
    )
    created_share_links: Mapped[list[ShareLink]] = relationship(back_populates="created_by_user")

    @property
    def has_password(self) -> bool:
        """Expose password availability without ever returning the password hash."""
        return self.password_hash is not None


class EmailOtp(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "email_otps"
    __table_args__ = (
        Index(
            "ix_email_otps_user_id_purpose_created_at_active",
            "user_id",
            "purpose",
            desc("created_at"),
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purpose: Mapped[EmailOtpPurpose] = mapped_column(
        domain_enum(EmailOtpPurpose, "email_otp_purpose"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="email_otps")
