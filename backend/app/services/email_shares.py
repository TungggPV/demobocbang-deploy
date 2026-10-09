"""Build safe, human-readable attachments for meeting invitation emails."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.media import MediaFile
from app.models.meeting import Meeting
from app.models.summary import Summary, SummaryVersion
from app.models.transcript import CurrentSegment, SegmentVersion, Speaker, Transcript
from app.models.translation import Translation, TranslationSegment
from app.providers.email import EmailAttachment
from app.providers.storage import MediaStorage, StorageError


async def build_meeting_email_attachments(
    session: AsyncSession,
    storage: MediaStorage,
    meeting: Meeting,
    selections: Sequence[str],
    max_bytes: int,
) -> list[EmailAttachment]:
    attachments: list[EmailAttachment] = []
    total_bytes = 0
    stem = _safe_stem(meeting.title)

    def append(filename: str, content: bytes, content_type: str) -> None:
        nonlocal total_bytes
        total_bytes += len(content)
        if total_bytes > max_bytes:
            raise ApiError(
                "EMAIL_ATTACHMENT_TOO_LARGE",
                "Tổng dung lượng tệp đính kèm quá lớn để gửi email. "
                "Hãy bỏ bớt băng ghi âm hoặc nội dung khác.",
                422,
            )
        attachments.append(EmailAttachment(filename, content, content_type))

    if "audio" in selections:
        media = await session.scalar(
            select(MediaFile)
            .where(MediaFile.meeting_id == meeting.id, MediaFile.deleted_at.is_(None))
            .order_by(MediaFile.created_at.desc())
            .limit(1)
        )
        if media is None:
            raise ApiError(
                "EMAIL_AUDIO_UNAVAILABLE",
                "Cuộc họp chưa có băng ghi âm để đính kèm.",
                422,
            )
        if media.size_bytes + total_bytes > max_bytes:
            raise ApiError(
                "EMAIL_ATTACHMENT_TOO_LARGE",
                "Băng ghi âm quá lớn để gửi qua email. Hãy bỏ chọn băng ghi âm và gửi lại.",
                422,
            )
        try:
            audio = await storage.get_bytes(media.storage_key)
        except StorageError as exc:
            raise ApiError(
                "EMAIL_AUDIO_UNAVAILABLE",
                "Không thể đọc băng ghi âm để đính kèm.",
                503,
            ) from exc
        append(_safe_filename(media.original_filename, f"{stem}-ghi-am"), audio, media.mime_type)

    if "transcript" in selections:
        transcript = await session.scalar(
            select(Transcript).where(Transcript.meeting_id == meeting.id)
        )
        if transcript is not None and transcript.current_projection_initialized_at is not None:
            segments = list(
                await session.scalars(
                    select(CurrentSegment)
                    .where(CurrentSegment.transcript_id == transcript.id)
                    .order_by(CurrentSegment.position)
                )
            )
        elif meeting.current_transcript_version_id is not None:
            segments = list(
                await session.scalars(
                    select(SegmentVersion)
                    .where(
                        SegmentVersion.transcript_version_id
                        == meeting.current_transcript_version_id
                    )
                    .order_by(SegmentVersion.sequence)
                )
            )
        else:
            segments = []
        if not segments:
            raise ApiError(
                "EMAIL_TRANSCRIPT_UNAVAILABLE",
                "Cuộc họp chưa có nội dung bóc băng để đính kèm.",
                422,
            )
        speakers = {
            item.id: item.name
            for item in await session.scalars(
                select(Speaker).where(Speaker.meeting_id == meeting.id)
            )
        }
        lines = [f"NỘI DUNG BÓC BĂNG — {meeting.title}", ""]
        for item in segments:
            speaker = speakers.get(item.speaker_id, "Chưa xác định")
            lines.append(
                f"[{_timestamp(item.start_ms)} – {_timestamp(item.end_ms)}] {speaker}: {item.text}"
            )
        append(
            f"{stem}-noi-dung.txt",
            ("\n".join(lines) + "\n").encode("utf-8"),
            "text/plain",
        )

    if "translation" in selections:
        translation = await session.scalar(
            select(Translation)
            .where(Translation.meeting_id == meeting.id)
            .order_by(Translation.created_at.desc())
            .limit(1)
        )
        if translation is None:
            raise ApiError(
                "EMAIL_TRANSLATION_UNAVAILABLE",
                "Cuộc họp chưa có bản dịch để đính kèm.",
                422,
            )
        rows = list(
            await session.scalars(
                select(TranslationSegment)
                .where(TranslationSegment.translation_id == translation.id)
                .order_by(TranslationSegment.sequence)
            )
        )
        if not rows:
            raise ApiError(
                "EMAIL_TRANSLATION_UNAVAILABLE",
                "Bản dịch hiện tại chưa có nội dung.",
                422,
            )
        lines = [
            f"BẢN DỊCH — {meeting.title}",
            f"{translation.source_language.upper()} → {translation.target_language.upper()}",
            "",
        ]
        lines.extend(f"{index}. {item.text}" for index, item in enumerate(rows, start=1))
        append(
            f"{stem}-ban-dich-{translation.target_language}.txt",
            ("\n".join(lines) + "\n").encode("utf-8"),
            "text/plain",
        )

    if "summary" in selections:
        summary = await session.scalar(
            select(Summary)
            .where(Summary.meeting_id == meeting.id)
            .order_by(Summary.created_at.desc())
            .limit(1)
        )
        version = (
            await session.scalar(
                select(SummaryVersion)
                .where(SummaryVersion.summary_id == summary.id)
                .order_by(SummaryVersion.version_no.desc())
                .limit(1)
            )
            if summary is not None
            else None
        )
        if summary is None or version is None:
            raise ApiError(
                "EMAIL_SUMMARY_UNAVAILABLE",
                "Cuộc họp chưa có tóm tắt để đính kèm.",
                422,
            )
        lines = [f"TÓM TẮT — {meeting.title}", ""]
        lines.extend(_summary_lines(version.content))
        append(
            f"{stem}-tom-tat.txt",
            ("\n".join(lines) + "\n").encode("utf-8"),
            "text/plain",
        )

    return attachments


def _summary_lines(content: dict[str, object]) -> list[str]:
    labels = {
        "overview": "Tổng quan",
        "key_points": "Ý chính",
        "action_items": "Công việc cần làm",
        "decisions": "Quyết định",
        "risks_notes": "Rủi ro và lưu ý",
    }
    lines: list[str] = []
    for key, value in content.items():
        lines.append(labels.get(key, key.replace("_", " ").capitalize()))
        if isinstance(value, list):
            lines.extend(f"- {item}" for item in value)
        elif isinstance(value, dict):
            lines.extend(f"- {child}: {item}" for child, item in value.items())
        else:
            lines.append(str(value))
        lines.append("")
    return lines


def _timestamp(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _safe_stem(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-") or "cuoc-hop"


def _safe_filename(value: str, fallback_stem: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\r\n\x00]+", "", name).strip()
    return name or f"{fallback_stem}.bin"
