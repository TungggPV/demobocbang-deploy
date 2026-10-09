"""Asynchronous, ACL-protected transcript exports."""

from __future__ import annotations

import io
import html
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.enums import ExportFormat, JobStatus, MeetingPermission
from app.models.export import Export
from app.models.media import Job
from app.models.meeting import Meeting
from app.models.summary import Summary, SummaryVersion
from app.models.transcript import SegmentVersion, Speaker, TranscriptVersion
from app.models.translation import Translation, TranslationSegment
from app.models.user import User
from app.providers.storage import MediaStorage, StorageError
from app.schemas.export import ExportCreateRequest, ExportResponse
from app.services.meetings import MeetingService
from app.services.transcripts import TranscriptService

_CONTENT_TYPES = {
    ExportFormat.TXT: "text/plain; charset=utf-8",
    ExportFormat.SRT: "application/x-subrip; charset=utf-8",
    ExportFormat.VTT: "text/vtt; charset=utf-8",
    ExportFormat.JSON: "application/json; charset=utf-8",
    ExportFormat.PDF: "application/pdf",
    ExportFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class ExportService:
    def __init__(self, session: AsyncSession, storage: MediaStorage) -> None:
        self._session, self._storage = session, storage
        self._meetings = MeetingService(session)

    async def request(
        self, meeting_id: UUID, user: User, payload: ExportCreateRequest
    ) -> ExportResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        version = await TranscriptService(self._session).ensure_current_snapshot(meeting)
        wants_translation = (
            payload.options.include_translation
            or payload.options.content_mode in {"translation", "bilingual"}
        )
        if wants_translation:
            translation = await self._session.scalar(
                select(Translation)
                .where(Translation.meeting_id == meeting.id)
                .order_by(Translation.created_at.desc())
                .limit(1)
            )
            if translation is None:
                raise ApiError(
                    "TRANSLATION_NOT_FOUND",
                    "Cuộc họp chưa có bản dịch để xuất.",
                    422,
                )
        export = Export(
            meeting_id=meeting.id, transcript_version_id=version.id, format=payload.format
        )
        self._session.add(export)
        await self._session.flush()
        job = Job(
            meeting_id=meeting.id,
            job_type="export",
            status=JobStatus.QUEUED,
            progress=0,
            payload={"export_id": str(export.id), "options": payload.options.model_dump()},
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(export)
        await self._session.refresh(job)
        return self._response(export, job)

    async def get(self, export_id: UUID, user: User) -> ExportResponse:
        export = await self._export_for_read(export_id, user)
        return self._response(export, await self._job(export.id))

    async def download(self, export_id: UUID, user: User) -> tuple[bytes, Export]:
        export = await self._export_for_read(export_id, user)
        job = await self._job(export.id)
        if job is None or job.status != JobStatus.COMPLETED or not export.storage_key:
            raise ApiError("EXPORT_NOT_READY", "The export is not ready to download.", 409)
        try:
            return await self._storage.get_bytes(export.storage_key), export
        except (OSError, StorageError) as exc:
            raise ApiError("EXPORT_STORAGE_ERROR", "The export file is unavailable.", 503) from exc

    async def presigned_download_url(self, export_id: UUID, user: User, expires_in: int) -> str | None:
        export = await self._export_for_read(export_id, user)
        job = await self._job(export.id)
        if job is None or job.status != JobStatus.COMPLETED or not export.storage_key:
            raise ApiError("EXPORT_NOT_READY", "The export is not ready to download.", 409)
        disposition = f'attachment; filename="{export.filename or "meeting-export"}"'
        try:
            return await self._storage.presigned_get_url(
                export.storage_key,
                expires_in,
                content_type=export.content_type,
                content_disposition=disposition,
            )
        except StorageError as exc:
            raise ApiError("EXPORT_STORAGE_ERROR", "The export URL could not be created.", 503) from exc

    async def purge_meeting_storage(self, meeting_id: UUID) -> None:
        keys = list(
            await self._session.scalars(
                select(Export.storage_key).where(
                    Export.meeting_id == meeting_id, Export.storage_key.is_not(None)
                )
            )
        )
        try:
            for key in keys:
                await self._storage.delete(key)
        except StorageError as exc:
            raise ApiError("EXPORT_STORAGE_ERROR", "Meeting exports could not be deleted.", 502) from exc

    async def _current_version(self, meeting: Meeting) -> TranscriptVersion:
        if meeting.current_transcript_version_id is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "A transcript is required for export.", 422)
        version = await self._session.get(TranscriptVersion, meeting.current_transcript_version_id)
        if version is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "A transcript is required for export.", 422)
        return version

    async def _export_for_read(self, export_id: UUID, user: User) -> Export:
        export = await self._session.get(Export, export_id)
        if export is None:
            raise ApiError("EXPORT_NOT_FOUND", "Export was not found.", 404)
        await self._meetings.require_access(export.meeting_id, user, MeetingPermission.READ)
        return export

    async def _job(self, export_id: UUID) -> Job | None:
        jobs = list(await self._session.scalars(select(Job).where(Job.job_type == "export")))
        return next(
            (job for job in jobs if (job.payload or {}).get("export_id") == str(export_id)), None
        )

    @staticmethod
    def _response(export: Export, job: Job | None) -> ExportResponse:
        return ExportResponse(
            id=export.id,
            meeting_id=export.meeting_id,
            transcript_version_id=export.transcript_version_id,
            format=export.format,
            job_id=job.id if job else None,
            status=job.status if job else JobStatus.FAILED,
            filename=export.filename,
            content_type=export.content_type,
            created_at=export.created_at,
        )


class ExportJobRunner:
    """Runs export jobs using the same durable ``jobs`` table as AI jobs."""

    def __init__(self, storage: MediaStorage) -> None:
        self._storage = storage

    async def run(self, session: AsyncSession, job: Job) -> None:
        payload = job.payload or {}
        export = await session.get(Export, UUID(str(payload["export_id"])))
        if export is None:
            raise ValueError("Export request no longer exists.")
        meeting = await session.get(Meeting, export.meeting_id)
        if meeting is None:
            raise ValueError("Meeting no longer exists.")
        segments = list(
            await session.scalars(
                select(SegmentVersion)
                .where(SegmentVersion.transcript_version_id == export.transcript_version_id)
                .order_by(SegmentVersion.sequence)
            )
        )
        if not segments:
            raise ValueError("Transcript input is empty or invalid.")
        options = {
            "include_timestamps": True,
            "include_speakers": True,
            "include_translation": False,
            "include_summary": False,
            "content_mode": "transcript",
            **dict(payload.get("options", {})),
        }
        if options["include_translation"] and options["content_mode"] == "transcript":
            options["content_mode"] = "bilingual"
        speakers = {
            item.id: item.name
            for item in await session.scalars(
                select(Speaker).where(Speaker.meeting_id == meeting.id)
            )
        }
        wants_translation = options["content_mode"] in {"translation", "bilingual"}
        translation, translation_is_current, translation_info = (
            await _translation_by_segment(session, meeting.id, export.transcript_version_id)
            if wants_translation
            else ({}, True, None)
        )
        summary = (
            await _latest_summary(session, meeting.id, export.transcript_version_id)
            if options["include_summary"]
            else None
        )
        document = _document(
            meeting,
            export,
            segments,
            speakers,
            translation,
            summary,
            options,
            translation_is_current,
            translation_info,
        )
        content = _render(export.format, document)
        filename = _filename(meeting.title, export.format, str(options["content_mode"]))
        key = f"exports/meetings/{meeting.id}/{export.id}.{export.format.value}"
        # Export binaries can be large. Keep the object-storage boundary
        # streaming-friendly and avoid assuming an API/worker shared volume.
        descriptor, temporary_name = tempfile.mkstemp(prefix="bocbang-export-", suffix=f".{export.format.value}")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            await self._storage.put_file(temporary, key)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        export.storage_key, export.filename, export.content_type = (
            key,
            filename,
            _CONTENT_TYPES[export.format],
        )


async def _translation_by_segment(
    session: AsyncSession, meeting_id: UUID, version_id: UUID
) -> tuple[dict[UUID, str], bool, dict[str, str] | None]:
    translation = await session.scalar(
        select(Translation)
        .where(Translation.meeting_id == meeting_id)
        .order_by(Translation.created_at.desc())
        .limit(1)
    )
    if translation is None:
        return {}, True, None
    rows = await session.scalars(
        select(TranslationSegment).where(TranslationSegment.translation_id == translation.id)
    )
    return (
        {row.logical_segment_id: row.text for row in rows},
        translation.transcript_version_id == version_id,
        {
            "source_language": translation.source_language,
            "target_language": translation.target_language,
        },
    )


async def _latest_summary(
    session: AsyncSession, meeting_id: UUID, version_id: UUID
) -> dict[str, object] | None:
    summary = await session.scalar(
        select(Summary)
        .where(Summary.meeting_id == meeting_id, Summary.transcript_version_id == version_id)
        .order_by(Summary.created_at.desc())
        .limit(1)
    )
    if summary is None:
        return None
    row = await session.scalar(
        select(SummaryVersion)
        .where(SummaryVersion.summary_id == summary.id)
        .order_by(SummaryVersion.version_no.desc())
        .limit(1)
    )
    return row.content if row else None


def _document(
    meeting: Meeting,
    export: Export,
    segments: list[SegmentVersion],
    speakers: dict[UUID, str],
    translation: dict[UUID, str],
    summary: dict[str, object] | None,
    options: dict[str, object],
    translation_is_current: bool = True,
    translation_info: dict[str, str] | None = None,
) -> dict[str, object]:
    data = {
        "meeting": {
            "title": meeting.title,
            "description": meeting.description,
            "language_mode": meeting.language_mode,
            "status": meeting.status.value,
            "source_type": meeting.source_type.value,
            "duration_ms": meeting.duration_ms,
            "created_at": meeting.created_at.isoformat(),
            "transcript_version_id": str(export.transcript_version_id),
        },
        "export": {
            "format": export.format.value,
            "created_at": datetime.now(UTC).isoformat(),
        },
        "options": options,
        "segments": [],
    }
    if translation_info is not None:
        data["translation"] = translation_info
    content_mode = str(options.get("content_mode", "transcript"))
    missing_translation = False
    for item in segments:
        row: dict[str, object] = {
            "id": str(item.logical_segment_id),
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "text": item.text,
        }
        if options["include_speakers"]:
            row["speaker"] = speakers.get(item.speaker_id) if item.speaker_id else None
        translated = translation.get(item.logical_segment_id)
        if content_mode == "translation":
            row["source_text"] = item.text
            row["text"] = translated or f"[Chưa có bản dịch] {item.text}"
            missing_translation = missing_translation or translated is None
        elif content_mode == "bilingual":
            if translated is not None:
                row["translation"] = translated
            else:
                missing_translation = True
        data["segments"].append(row)
    if options["include_summary"] and summary is not None:
        data["summary"] = summary
    warnings: list[str] = []
    if content_mode in {"translation", "bilingual"} and not translation:
        warnings.append("Chưa có bản dịch để đưa vào tệp.")
    elif content_mode in {"translation", "bilingual"} and not translation_is_current:
        warnings.append("Bản dịch được tạo từ phiên bản nội dung trước; hãy dịch lại nếu nội dung đã thay đổi.")
    if missing_translation:
        warnings.append("Một số đoạn mới chưa có bản dịch tương ứng.")
    if options["include_summary"] and summary is None:
        warnings.append("Chưa có tóm tắt phù hợp với phiên bản nội dung hiện tại.")
    if warnings:
        data["warnings"] = warnings
    return data


def _render(format: ExportFormat, data: dict[str, object]) -> bytes:
    if format == ExportFormat.JSON:
        payload = dict(data)
        payload["segments"] = []
        for item in data["segments"]:
            row = dict(item)
            if not data["options"]["include_timestamps"]:
                row.pop("start_ms", None)
                row.pop("end_ms", None)
            payload["segments"].append(row)
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if format in (ExportFormat.SRT, ExportFormat.VTT):
        return _subtitles(data, format).encode("utf-8")
    text = _plain_text(data)
    if format == ExportFormat.TXT:
        return text.encode("utf-8")
    if format == ExportFormat.DOCX:
        from docx import Document
        from docx.shared import Pt

        document = Document()
        document.add_heading(str(data["meeting"]["title"]), 0)
        meeting = data["meeting"]
        if meeting.get("description"):
            document.add_paragraph(str(meeting["description"]))
        document.add_heading("Thông tin", level=1)
        table = document.add_table(rows=0, cols=2)
        table.style = "Light Shading Accent 1"
        for label, value in _meeting_details(meeting):
            cells = table.add_row().cells
            cells[0].text, cells[1].text = label, value
        document.add_heading(_content_heading(data["options"]), level=1)
        for index, item in enumerate(data["segments"], start=1):
            paragraph = document.add_paragraph()
            prefix = _segment_prefix(item, data["options"], index)
            paragraph.add_run(prefix).bold = True
            paragraph.add_run(str(item["text"]))
            if item.get("translation"):
                translated = document.add_paragraph(style="Intense Quote")
                translated.add_run("Bản dịch: ").bold = True
                translated.add_run(str(item["translation"]))
        if data.get("summary"):
            document.add_heading("Tóm tắt", level=1)
            for heading, values in _summary_sections(data["summary"]):
                document.add_heading(heading, level=2)
                for value in values:
                    document.add_paragraph(value, style="List Bullet" if len(values) > 1 else None)
        for warning in data.get("warnings", []):
            document.add_paragraph(str(warning), style="Intense Quote")
        styles = document.styles
        styles["Normal"].font.name = "Arial"
        styles["Normal"].font.size = Pt(10.5)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    # DejaVu is shipped by many Linux images; failure is a stable failed job rather than mojibake.
    font = next(
        (
            path
            for path in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "C:/Windows/Fonts/arial.ttf",
            )
            if os.path.exists(path)
        ),
        None,
    )
    if font is None:
        raise ValueError("A Unicode PDF font is not configured.")
    pdfmetrics.registerFont(TTFont("ExportUnicode", font))
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=17 * mm, bottomMargin=17 * mm, title=str(data["meeting"]["title"]))
    base = getSampleStyleSheet()
    body = ParagraphStyle("ExportBody", parent=base["BodyText"], fontName="ExportUnicode", fontSize=10.5, leading=16, spaceAfter=5)
    title = ParagraphStyle("ExportTitle", parent=body, fontSize=20, leading=25, alignment=TA_CENTER, spaceAfter=14)
    heading = ParagraphStyle("ExportHeading", parent=body, fontSize=13, leading=18, spaceBefore=10, spaceAfter=6)
    story = [Paragraph(html.escape(str(data["meeting"]["title"])), title)]
    for line in text.splitlines()[2:]:
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 4 * mm))
        elif stripped in {"THÔNG TIN", "NỘI DUNG BÓC BĂNG", "BẢN DỊCH", "NỘI DUNG SONG NGỮ", "TÓM TẮT", "LƯU Ý"}:
            story.append(Paragraph(stripped, heading))
        else:
            story.append(Paragraph(html.escape(line).replace("  ", "&nbsp;&nbsp;"), body))
    pdf.build(story)
    return buffer.getvalue()


def _plain_text(data: dict[str, object]) -> str:
    meeting = data["meeting"]
    mode = str(data["options"].get("content_mode", "transcript"))
    document_title = {
        "translation": "BẢN DỊCH CUỘC HỌP",
        "bilingual": "NỘI DUNG SONG NGỮ",
    }.get(mode, "BẢN GHI CUỘC HỌP")
    lines = [document_title, str(meeting["title"]), "", "THÔNG TIN"]
    lines.extend(f"{label}: {value}" for label, value in _meeting_details(meeting))
    if meeting.get("description"):
        lines.extend(["", "Mô tả", str(meeting["description"])])
    lines.extend(["", _content_heading(data["options"]).upper()])
    for index, item in enumerate(data["segments"], start=1):
        lines.append(_segment_prefix(item, data["options"], index) + str(item["text"]))
        if item.get("translation"):
            lines.append(f"   Bản dịch: {item['translation']}")
    if data.get("summary"):
        lines.extend(["", "TÓM TẮT"])
        for heading, values in _summary_sections(data["summary"]):
            lines.append(heading)
            lines.extend(f"- {value}" for value in values)
    if data.get("warnings"):
        lines.extend(["", "LƯU Ý"])
        lines.extend(f"- {value}" for value in data["warnings"])
    return "\n".join(lines) + "\n"


def _subtitles(data: dict[str, object], format: ExportFormat) -> str:
    lines = ["WEBVTT", ""] if format == ExportFormat.VTT else []
    for index, item in enumerate(data["segments"], start=1):
        if format == ExportFormat.SRT:
            lines.append(str(index))
        separator = "." if format == ExportFormat.VTT else ","
        lines.append(
            f"{_timestamp(item['start_ms'], separator)} --> {_timestamp(item['end_ms'], separator)}"
        )
        label = f"{item['speaker']}: " if item.get("speaker") else ""
        lines.append(label + str(item["text"]))
        if item.get("translation"):
            lines.append(str(item["translation"]))
        lines.append("")
    return "\n".join(lines)


def _timestamp(value: object, separator: str = ".") -> str:
    ms = int(value)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"


def _filename(title: str, format: ExportFormat, content_mode: str = "transcript") -> str:
    stem = re.sub(r"[^\w.-]+", "-", title, flags=re.UNICODE).strip(".-") or "meeting"
    suffix = {"translation": "-ban-dich", "bilingual": "-song-ngu"}.get(content_mode, "")
    return f"{stem}{suffix}.{format.value}"


def _meeting_details(meeting: dict[str, object]) -> list[tuple[str, str]]:
    language = {"vi": "Tiếng Việt", "en": "Tiếng Anh", "auto": "Tự động nhận diện"}.get(str(meeting["language_mode"]), str(meeting["language_mode"]))
    created = str(meeting["created_at"]).replace("T", " ").replace("+00:00", " UTC")
    status = {"created": "Chưa bắt đầu", "recording": "Đang ghi âm", "processing": "Đang xử lý", "completed": "Hoàn tất", "failed": "Thất bại"}.get(str(meeting.get("status")), str(meeting.get("status", "")))
    source = {"realtime": "Ghi âm trực tiếp", "upload": "Tệp tải lên"}.get(str(meeting.get("source_type")), str(meeting.get("source_type", "")))
    return [("Trạng thái", status), ("Nguồn", source), ("Ngôn ngữ", language), ("Ngày tạo", created), ("Phiên bản nội dung", str(meeting["transcript_version_id"]))]


def _content_heading(options: dict[str, object]) -> str:
    return {
        "translation": "Bản dịch",
        "bilingual": "Nội dung song ngữ",
    }.get(str(options.get("content_mode", "transcript")), "Nội dung bóc băng")


def _segment_prefix(item: dict[str, object], options: dict[str, object], index: int) -> str:
    parts = [f"{index}."]
    if options["include_timestamps"]:
        parts.append(f"[{_timestamp(item['start_ms'])} – {_timestamp(item['end_ms'])}]")
    if options["include_speakers"]:
        parts.append(f"{item.get('speaker') or 'Chưa xác định'}:")
    return " ".join(parts) + " "


def _summary_sections(summary: object) -> list[tuple[str, list[str]]]:
    if not isinstance(summary, dict):
        return [("Nội dung", [str(summary)])]
    labels = {"overview": "Tổng quan", "key_points": "Ý chính", "action_items": "Công việc cần làm", "decisions": "Quyết định", "risks_notes": "Rủi ro và lưu ý"}
    sections: list[tuple[str, list[str]]] = []
    for key, value in summary.items():
        heading = labels.get(str(key), str(key).replace("_", " ").capitalize())
        if isinstance(value, list):
            values = [str(item) for item in value if str(item).strip()]
        elif isinstance(value, dict):
            values = [f"{child}: {content}" for child, content in value.items()]
        else:
            values = [str(value)] if str(value).strip() else []
        if values:
            sections.append((heading, values))
    return sections
