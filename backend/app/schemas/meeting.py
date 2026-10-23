"""HTTP schemas for meeting ownership, ACL members, and share links."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import MeetingPermission, MeetingSourceType, MeetingStatus
from app.schemas.auth import UserResponse, _normalize_email


AccessLevel = MeetingPermission | Literal["owner"]


class MeetingCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=20_000)
    source_type: MeetingSourceType
    language_mode: str = Field(min_length=1, max_length=35)
    diarization_enabled: bool = False

    @field_validator("title", "language_mode")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required.")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class MeetingUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=20_000)
    language_mode: str | None = Field(default=None, min_length=1, max_length=35)
    diarization_enabled: bool | None = None

    @field_validator("title", "language_mode")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "MeetingUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


class MeetingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    source_type: MeetingSourceType
    language_mode: str
    diarization_enabled: bool
    status: MeetingStatus
    duration_ms: int | None
    created_at: datetime
    updated_at: datetime
    permission: AccessLevel


class MeetingMemberCreateRequest(BaseModel):
    """Invite an already registered user by either stable id or email."""

    user_id: UUID | None = None
    email: str | None = Field(default=None, min_length=3, max_length=320)
    permission: MeetingPermission

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return _normalize_email(value) if value is not None else None

    @model_validator(mode="after")
    def require_one_user_reference(self) -> "MeetingMemberCreateRequest":
        if (self.user_id is None) == (self.email is None):
            raise ValueError("Provide exactly one of user_id or email.")
        return self


class MeetingMemberUpdateRequest(BaseModel):
    permission: MeetingPermission


class MeetingMemberResponse(BaseModel):
    user_id: UUID
    permission: AccessLevel
    created_at: datetime
    user: UserResponse


class ShareLinkCreateRequest(BaseModel):
    permission: MeetingPermission
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_future_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone.")
        if value is not None and value <= datetime.now(value.tzinfo):
            raise ValueError("expires_at must be in the future.")
        return value


class ShareLinkUpdateRequest(BaseModel):
    permission: MeetingPermission | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ShareLinkUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self

    @field_validator("expires_at")
    @classmethod
    def require_future_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone.")
        if value is not None and value <= datetime.now(value.tzinfo):
            raise ValueError("expires_at must be in the future.")
        return value


class ShareLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    permission: MeetingPermission
    expires_at: datetime | None
    revoked_at: datetime | None
    created_by: UUID
    created_at: datetime


class ShareLinkCreatedResponse(ShareLinkResponse):
    """The raw token is deliberately returned once, at creation only."""

    token: str


from app.schemas.transcript import SpeakerResponse, TranscriptResponse
from app.schemas.media import MediaFileResponse
from app.schemas.ai import TranslationResponse


class MeetingBundleResponse(BaseModel):
    meeting: MeetingResponse
    transcript: TranscriptResponse | None = None
    media: list[MediaFileResponse]
    speakers: list[SpeakerResponse]
    translations: list[TranslationResponse]
