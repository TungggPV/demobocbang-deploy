"""Named agenda sessions on the retained meeting audio timeline."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MeetingSection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_sections"
    __table_args__ = (
        UniqueConstraint("meeting_id", "sequence", name="uq_meeting_section_sequence"),
        CheckConstraint(
            "start_ms >= 0 AND (end_ms IS NULL OR end_ms >= start_ms)",
            name="ck_meeting_section_interval",
        ),
    )

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
