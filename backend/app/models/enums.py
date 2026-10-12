"""Database-backed domain enumerations shared by ORM models."""

from enum import StrEnum

from sqlalchemy import Enum


class EmailOtpPurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class MeetingPermission(StrEnum):
    READ = "read"
    COMMENT = "comment"
    EDIT = "edit"


class MeetingSourceType(StrEnum):
    REALTIME = "realtime"
    UPLOAD = "upload"


class MeetingStatus(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    PROCESSING = "processing"
    READY = "ready"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class TranscriptVersionSource(StrEnum):
    STT = "stt"
    USER_EDIT = "user_edit"


class SummaryTemplateType(StrEnum):
    GENERAL = "general"
    BRIEFING = "briefing"
    VOTING = "voting"


class ExportFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


def domain_enum(enum_class: type[StrEnum], name: str) -> Enum:
    """Build a native PostgreSQL enum that persists public string values."""
    return Enum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
