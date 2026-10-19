"""Uploaded media and asynchronous job persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import JobStatus, domain_enum
from app.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.meeting import Meeting


class MediaFile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "media_files"

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    normalized_storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="media_files")


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    meeting_id: Mapped[UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_file_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, default="media_processing")
    status: Mapped[JobStatus] = mapped_column(
        domain_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.QUEUED, index=True
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="jobs")


class StorageBlob(Base):
    __tablename__ = "storage_blobs"

    storage_key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
