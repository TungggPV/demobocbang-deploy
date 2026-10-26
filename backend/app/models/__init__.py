"""Import mapped classes so SQLAlchemy metadata is complete for Alembic."""

from app.models.collaboration import Comment
from app.models.export import Export
from app.models.media import Job, MediaFile, MeetingAudioChunk, StorageBlob
from app.models.meeting import Meeting, MeetingMember, ShareLink
from app.models.meeting_section import MeetingSection
from app.models.summary import Summary, SummaryVersion
from app.models.transcript import SegmentVersion, Speaker, Transcript, TranscriptVersion
from app.models.translation import Translation, TranslationSegment
from app.models.user import EmailOtp, User
from app.models.voice_profile import VoiceProfile

__all__ = [
    "Comment",
    "EmailOtp",
    "Export",
    "Job",
    "MediaFile",
    "MeetingAudioChunk",
    "Meeting",
    "MeetingMember",
    "MeetingSection",
    "SegmentVersion",
    "ShareLink",
    "Speaker",
    "StorageBlob",
    "Summary",
    "SummaryVersion",
    "Transcript",
    "TranscriptVersion",
    "Translation",
    "TranslationSegment",
    "User",
    "VoiceProfile",
]
