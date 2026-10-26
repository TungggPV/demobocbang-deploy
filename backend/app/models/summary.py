"""Versioned summaries tied to an immutable transcript input."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SummaryTemplateType, domain_enum
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.meeting import Meeting
    from app.models.transcript import TranscriptVersion
    from app.models.user import User


class Summary(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "summaries"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id",
            "transcript_version_id",
            "scope",
            "mode",
            "template_type",
            "output_language",
            name="uq_summary_input_scope_mode_language",
        ),
    )

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transcript_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("transcript_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_type: Mapped[SummaryTemplateType] = mapped_column(
        domain_enum(SummaryTemplateType, "summary_template_type"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="template")
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="meeting", server_default="meeting")
    output_language: Mapped[str] = mapped_column(String(2), nullable=False)

    meeting: Mapped[Meeting] = relationship(back_populates="summaries")
    transcript_version: Mapped[TranscriptVersion] = relationship(back_populates="summaries")
    versions: Mapped[list[SummaryVersion]] = relationship(
        back_populates="summary", cascade="all, delete-orphan", passive_deletes=True
    )


class SummaryVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "summary_versions"
    __table_args__ = (UniqueConstraint("summary_id", "version_no", name="uq_summary_version_no"),)

    summary_id: Mapped[UUID] = mapped_column(
        ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    summary: Mapped[Summary] = relationship(back_populates="versions")
    created_by_user: Mapped[User | None] = relationship()
