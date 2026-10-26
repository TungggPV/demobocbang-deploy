"""Public schemas for asynchronous translations and AI summaries."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import SummaryTemplateType


class TranslationCreateRequest(BaseModel):
    source_language: str = Field(min_length=2, max_length=35)
    target_language: str = Field(min_length=2, max_length=35)

    @field_validator("source_language", "target_language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return value.strip().lower()


class TranslationSegmentResponse(BaseModel):
    logical_segment_id: UUID
    sequence: int
    text: str


class TranslationResponse(BaseModel):
    id: UUID
    meeting_id: UUID
    transcript_version_id: UUID
    source_language: str
    target_language: str
    created_at: datetime
    segments: list[TranslationSegmentResponse]


class SummaryCreateRequest(BaseModel):
    scope: Literal["meeting", "sessions"] = "meeting"
    section_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=1000)
    sections_revision: str | None = Field(default=None, max_length=64)
    mode: Literal["template", "custom"] = "template"
    template: SummaryTemplateType | None = None
    prompt: str | None = Field(default=None, max_length=4000)
    output_language: Literal["vi", "en"]

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        prompt = value.strip()
        return prompt or None

    @model_validator(mode="after")
    def validate_summary_mode(self) -> SummaryCreateRequest:
        if self.prompt is not None and self.template is None and self.mode == "template":
            self.mode = "custom" 
        if self.scope != "sessions" and (self.section_ids is not None or self.sections_revision is not None):
            raise ValueError("Session selection requires sessions scope.")
        if self.section_ids is not None and len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("Session IDs must be unique.")
        if self.mode == "template" and self.template is None:
            raise ValueError("A built-in summary template is required.")
        if self.mode == "template" and self.prompt is not None:
            raise ValueError("A built-in summary request must not include a custom prompt.")
        if self.mode == "custom" and self.prompt is None:
            raise ValueError("A custom summary prompt is required.")
        if self.mode == "custom" and self.template is not None:
            raise ValueError("A custom summary request must not include a built-in template.")
        return self


class SummaryEditRequest(BaseModel):
    content: dict[str, object]
    expected_version_no: int | None = Field(default=None, ge=1)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: dict[str, object]) -> dict[str, object]:
        if not value:
            raise ValueError("Summary content must not be empty.")
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 100_000:
            raise ValueError("Summary content is too large.")
        return value


class SummaryVersionResponse(BaseModel):
    id: UUID
    version_no: int
    prompt: str = ""
    content: dict[str, object]
    provider: str
    model: str
    created_by: UUID | None
    created_at: datetime


class SummaryResponse(BaseModel):
    id: UUID
    meeting_id: UUID
    transcript_version_id: UUID
    scope: Literal["meeting", "sessions"] = "meeting"
    mode: Literal["template", "custom"] = "template"
    template: SummaryTemplateType | None
    output_language: Literal["vi", "en"]
    current_version: SummaryVersionResponse
