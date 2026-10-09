"""Export request and response contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import ExportFormat, JobStatus


class ExportOptions(BaseModel):
    include_timestamps: bool = True
    include_speakers: bool = True
    include_translation: bool = False
    include_summary: bool = False
    content_mode: Literal["transcript", "translation", "bilingual"] = "transcript"


class ExportCreateRequest(BaseModel):
    format: ExportFormat
    options: ExportOptions = Field(default_factory=ExportOptions)


class ExportResponse(BaseModel):
    id: UUID
    meeting_id: UUID
    transcript_version_id: UUID
    format: ExportFormat
    job_id: UUID | None = None
    status: JobStatus
    filename: str | None = None
    content_type: str | None = None
    created_at: datetime
