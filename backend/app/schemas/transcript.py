"""Request and response schemas for transcript snapshots and speakers."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TranscriptVersionSource


class SpeakerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    provider_speaker_id: str | None
    name: str
    role: str | None
    organization: str | None
    is_identified: bool
    voice_profile_id: UUID | None = None
    match_confidence: float | None = None
    created_at: datetime
    updated_at: datetime


class SpeakerSampleResponse(BaseModel):
    """A real meeting-media interval; the client fetches media through its ACL route."""

    media_id: UUID
    start_ms: int
    end_ms: int


class SpeakerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    voice_profile_id: UUID | None = Field(default=None)


class SpeakerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    voice_profile_id: UUID | None = Field(default=None)

    @model_validator(mode="after")
    def require_change(self) -> "SpeakerUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


class SegmentResponse(BaseModel):
    id: UUID
    logical_segment_id: UUID
    start_ms: int
    end_ms: int
    text: str
    speaker_id: UUID | None
    confidence: float | None


class TranscriptResponse(BaseModel):
    version_id: UUID
    version_no: int
    language: str
    segments: list[SegmentResponse]
    next_cursor: str | None = None


class SharedSegmentResponse(BaseModel):
    logical_segment_id: UUID
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None = None
    confidence: float | None = None


class SharedTranscriptResponse(BaseModel):
    version_id: UUID
    version_no: int
    language: str
    segments: list[SharedSegmentResponse]


class TranscriptVersionResponse(BaseModel):
    id: UUID
    version_no: int
    source: TranscriptVersionSource
    parent_version_id: UUID | None
    created_by: UUID | None
    created_at: datetime


class SegmentEditRequest(BaseModel):
    text: str | None = Field(default=None, min_length=1)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_change(self) -> "SegmentEditRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        if self.start_ms is not None and self.end_ms is not None and self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be earlier than end_ms.")
        return self


class SegmentSplitRequest(BaseModel):
    split_at_ms: int = Field(ge=0)
    first_text: str | None = Field(default=None, min_length=1)
    second_text: str | None = Field(default=None, min_length=1)


class SegmentMergeRequest(BaseModel):
    first_segment_id: UUID | None = None
    second_segment_id: UUID | None = None
    segment_ids: list[UUID] | None = None

    @model_validator(mode="after")
    def validate_segments(self) -> SegmentMergeRequest:
        if self.segment_ids is not None and len(self.segment_ids) >= 2:
            return self
        if self.first_segment_id is not None and self.second_segment_id is not None:
            self.segment_ids = [self.first_segment_id, self.second_segment_id]
            return self
        raise ValueError("Cần cung cấp ít nhất 2 đoạn để gộp.")


class SegmentSpeakerRequest(BaseModel):
    speaker_id: UUID | None = None


class TranscriptMutationResponse(BaseModel):
    version_id: UUID
    version_no: int
    segment: SegmentResponse | None = None


class TranscriptRollbackRequest(BaseModel):
    target_version_id: UUID
