from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SectionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def normalize(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Session title cannot be blank.")
        return value


class MeetingSectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    meeting_id: UUID
    sequence: int
    title: str
    start_ms: int
    end_ms: int | None
    is_started: bool
    revision: str | None = None


class SectionCreateRequest(SectionRenameRequest):
    start_ms: int = Field(ge=0, strict=True)
    expected_revision: str = Field(max_length=64)


class SectionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)
    expected_revision: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_update(self):
        if self.title is not None:
            self.title = SectionRenameRequest(title=self.title).title
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("Title cannot be null.")
        if "start_ms" in self.model_fields_set and self.start_ms is None:
            raise ValueError("Start cannot be null.")
        if not self.model_fields_set.intersection({"title", "start_ms", "end_ms"}):
            raise ValueError("Provide a title or boundary to update.")
        if (
            self.model_fields_set.intersection({"start_ms", "end_ms"})
            and self.expected_revision is None
        ):
            raise ValueError("Reload the session layout before editing boundaries.")
        return self
