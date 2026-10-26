"""Persisted AI proposal and human-reviewed text, separate from current transcript."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.media import JobResponse


class PolishText(BaseModel):
    logical_segment_id: UUID
    text: str = Field(min_length=1, max_length=50000)


class PolishReviewRequest(BaseModel):
    revision: int = Field(ge=0)
    segments: list[PolishText] = Field(min_length=1, max_length=20000)


class PolishSegment(BaseModel):
    logical_segment_id: UUID
    speaker_id: UUID | None
    start_ms: int
    end_ms: int
    original_text: str
    generated_text: str
    text: str


class PolishResponse(BaseModel):
    job: JobResponse
    source_version_id: UUID
    accepted_version_id: UUID | None = None
    revision: int
    segments: list[PolishSegment]
