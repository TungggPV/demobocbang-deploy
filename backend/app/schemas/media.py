"""Response models for uploaded media and durable background jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import JobStatus


class MediaFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    original_filename: str
    mime_type: str
    size_bytes: int
    duration_ms: int
    status: str
    created_at: datetime


class MediaPlaybackResponse(BaseModel):
    url: str
    expires_in_seconds: int


class JobErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool | None = None


class JobResponse(BaseModel):
    id: UUID
    meeting_id: UUID
    media_file_id: UUID | None
    job_type: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    error: JobErrorResponse | None
    created_at: datetime
    updated_at: datetime


class RecordingResponse(BaseModel):
    status: Literal["not_completed", "unavailable", "pending", "ready", "failed", "deleted"]
    media: MediaFileResponse | None = None
    job: JobResponse | None = None
    can_prepare: bool = False
    warning: str | None = None
