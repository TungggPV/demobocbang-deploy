"""Pydantic schemas for voice profiles and enrolled speaker identification."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VoiceSampleItem(BaseModel):
    index: int
    has_audio: bool = True


class VoiceProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    meeting_id: UUID | None = None
    name: str
    sample_duration_ms: int
    provider: str
    sample_count: int
    has_audio: bool = False
    samples: list[VoiceSampleItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VoiceProfileCandidateResponse(BaseModel):
    id: UUID
    name: str
    sample_duration_ms: int
    provider: str
    sample_count: int
    has_audio: bool = False
    is_user_profile: bool


class VoiceProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MeetingVoiceSampleRequest(BaseModel):
    segment_ids: list[UUID] = Field(min_length=1, max_length=50)


class MeetingVoiceLearningRequest(MeetingVoiceSampleRequest):
    name: str = Field(min_length=1, max_length=255)
    profile_id: UUID | None = None
