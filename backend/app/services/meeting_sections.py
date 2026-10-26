"""Named timeline groups, independent of transcript and audio content."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.enums import MeetingPermission, MeetingStatus
from app.models.media import MediaFile, MeetingAudioChunk
from app.models.meeting import Meeting
from app.models.meeting_section import MeetingSection
from app.models.transcript import CurrentSegment, SegmentVersion, Transcript
from app.models.user import User
from app.schemas.meeting_section import (
    MeetingSectionResponse,
    SectionCreateRequest,
    SectionUpdateRequest,
)
from app.services.meetings import MeetingService


class MeetingSectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def rows(self, meeting_id: UUID) -> list[MeetingSection]:
        return list(
            await self.session.scalars(
                select(MeetingSection)
                .where(
                    MeetingSection.meeting_id == meeting_id,
                )
                .order_by(MeetingSection.sequence)
                .execution_options(populate_existing=True)
            )
        )

    async def list(self, meeting_id: UUID, actor: User) -> list[MeetingSectionResponse]:
        await MeetingService(self.session).require_access(meeting_id, actor, MeetingPermission.READ)
        return section_responses(await self.rows(meeting_id))

    async def _editable(self, meeting_id: UUID, actor: User, expected: str | None):
        meeting, _ = await MeetingService(self.session).require_access(
            meeting_id, actor, MeetingPermission.EDIT
        )
        await self.session.refresh(meeting, with_for_update=True)
        rows = await self.rows(meeting_id)
        check_sections_revision(rows, expected)
        return meeting, rows

    async def _duration(self, meeting: Meeting) -> int:
        """Read-only bounds; never change transcript, audio or meeting state."""
        ends = [meeting.duration_ms or 0]
        ends.append(
            await self.session.scalar(
                select(func.max(CurrentSegment.end_ms))
                .join(Transcript, CurrentSegment.transcript_id == Transcript.id)
                .where(Transcript.meeting_id == meeting.id)
            )
            or 0
        )
        if meeting.current_transcript_version_id:
            ends.append(
                await self.session.scalar(
                    select(func.max(SegmentVersion.end_ms)).where(
                        SegmentVersion.transcript_version_id
                        == meeting.current_transcript_version_id
                    )
                )
                or 0
            )
        ends.append(
            await self.session.scalar(
                select(func.max(MeetingAudioChunk.end_ms)).where(
                    MeetingAudioChunk.meeting_id == meeting.id
                )
            )
            or 0
        )
        ends.append(
            await self.session.scalar(
                select(func.max(MediaFile.duration_ms)).where(
                    MediaFile.meeting_id == meeting.id, MediaFile.deleted_at.is_(None)
                )
            )
            or 0
        )
        return max(ends)

    async def _renumber(self, rows: list[MeetingSection]) -> None:
        # Two flushes avoid the unique (meeting_id, sequence) constraint while moving rows.
        offset = max(row.sequence for row in rows) + len(rows) + 1
        for index, row in enumerate(rows):
            row.sequence = offset + index
        await self.session.flush()
        for index, row in enumerate(rows, 1):
            row.sequence = index
        await self.session.flush()

    async def create(self, meeting_id: UUID, actor: User, payload: SectionCreateRequest):
        meeting, rows = await self._editable(meeting_id, actor, payload.expected_revision)
        duration = await self._duration(meeting)
        if not rows:
            if payload.start_ms != 0:
                raise ApiError(
                    "SECTION_BOUNDARY_INVALID", "Khởi tạo phiên đầu tiên tại 00:00 trước.", 422
                )
            row = MeetingSection(
                meeting_id=meeting_id,
                sequence=1,
                title=payload.title,
                start_ms=0,
                end_ms=duration if meeting.status == MeetingStatus.COMPLETED else None,
                is_started=duration > 0,
            )
            self.session.add(row)
            rows = [row]
        else:
            if len(rows) >= 1000:
                raise ApiError("SECTION_LIMIT", "Cuộc họp đã đạt giới hạn 1000 phiên.", 422)
            boundary = payload.start_ms
            if (
                boundary <= 0
                or boundary >= duration
                or any(row.start_ms == boundary for row in rows)
            ):
                raise ApiError(
                    "SECTION_BOUNDARY_INVALID",
                    "Mốc mới phải nằm trong thời lượng đã ghi và không trùng mốc phiên.",
                    422,
                )
            index = max(i for i, row in enumerate(rows) if row.start_ms < boundary)
            previous = rows[index]
            if previous.end_ms is not None and boundary >= previous.end_ms:
                raise ApiError(
                    "SECTION_BOUNDARY_INVALID", "Mốc mới phải nằm bên trong một phiên hiện có.", 422
                )
            row = MeetingSection(
                meeting_id=meeting_id,
                sequence=len(rows) + 1,
                title=payload.title,
                start_ms=boundary,
                end_ms=previous.end_ms,
                is_started=True,
            )
            previous.end_ms = boundary
            self.session.add(row)
            rows.insert(index + 1, row)
            await self.session.flush()
            await self._renumber(rows)
        await self.session.commit()
        return section_responses(await self.rows(meeting_id))

    async def update(
        self, meeting_id: UUID, section_id: UUID, actor: User, payload: SectionUpdateRequest
    ):
        meeting, rows = await self._editable(meeting_id, actor, payload.expected_revision)
        index = next((i for i, row in enumerate(rows) if row.id == section_id), None)
        if index is None:
            raise ApiError("MEETING_SECTION_NOT_FOUND", "Meeting session was not found.", 404)
        row = rows[index]
        start = payload.start_ms if "start_ms" in payload.model_fields_set else row.start_ms
        end = payload.end_ms if "end_ms" in payload.model_fields_set else row.end_ms
        if (start, end) != (row.start_ms, row.end_ms):
            duration = await self._duration(meeting)
            if index == 0 and start != 0 or index == len(rows) - 1 and end != row.end_ms:
                raise ApiError(
                    "SECTION_BOUNDARY_INVALID",
                    "Giữ mốc đầu 00:00 và mốc kết thúc cuộc họp; chỉ sửa ranh giới giữa các phiên.",
                    422,
                )
            if (
                start > duration
                or (end is not None and (end > duration or end <= start))
                or (index > 0 and start <= rows[index - 1].start_ms)
                or (
                    index < len(rows) - 1
                    and (
                        end is None
                        or (
                            rows[index + 1].end_ms is not None
                            and end >= rows[index + 1].end_ms
                            and not (
                                end == rows[index + 1].end_ms
                                and not rows[index + 1].is_started
                                and index == len(rows) - 2
                            )
                        )
                    )
                )
            ):
                raise ApiError(
                    "SECTION_BOUNDARY_INVALID",
                    "Mốc phiên phải tăng dần, không rỗng, không vượt thời lượng đã ghi.",
                    422,
                )
            if index > 0:
                rows[index - 1].end_ms = start
            if index < len(rows) - 1:
                rows[index + 1].start_ms = end
                rows[index + 1].is_started |= end < duration
            row.start_ms, row.end_ms = start, end
            row.is_started |= start < duration
        if payload.title is not None:
            row.title = payload.title
        await self.session.commit()
        return next(
            item for item in section_responses(await self.rows(meeting_id)) if item.id == section_id
        )

    async def delete(self, meeting_id: UUID, section_id: UUID, actor: User, expected: str):
        _, rows = await self._editable(meeting_id, actor, expected)
        index = next((i for i, row in enumerate(rows) if row.id == section_id), None)
        if index is None:
            raise ApiError("MEETING_SECTION_NOT_FOUND", "Meeting session was not found.", 404)
        if len(rows) == 1:
            raise ApiError(
                "SECTION_REQUIRED", "Giữ ít nhất một phiên để bao phủ toàn bộ cuộc họp.", 422
            )
        removed = rows.pop(index)
        if index:
            rows[index - 1].end_ms = removed.end_ms
            rows[index - 1].is_started |= removed.is_started
        else:
            rows[0].start_ms = 0
            rows[0].is_started |= removed.is_started
        await self.session.delete(removed)  # Only a grouping row; no transcript/audio relations.
        await self.session.flush()
        await self._renumber(rows)
        await self.session.commit()
        return section_responses(await self.rows(meeting_id))

    async def ensure(self, meeting: Meeting) -> MeetingSection:
        """Caller holds meeting lock. Legacy data with no rows is one session."""
        rows = await self.rows(meeting.id)
        if rows:
            return rows[-1]
        row = MeetingSection(
            meeting_id=meeting.id,
            sequence=1,
            title="Phiên 1",
            start_ms=0,
            is_started=bool(meeting.current_transcript_version_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def begin(self, meeting: Meeting) -> None:
        await self.session.refresh(meeting, with_for_update=True)
        if meeting.status == MeetingStatus.COMPLETED:
            raise ApiError("MEETING_COMPLETED", "This meeting has already ended.", 409)
        row = await self.ensure(meeting)
        row.is_started = True
        await self.session.flush()

    async def split(self, meeting: Meeting, boundary_ms: int) -> MeetingSection:
        await self.session.refresh(meeting, with_for_update=True)
        if meeting.status == MeetingStatus.COMPLETED:
            raise ApiError("MEETING_COMPLETED", "This meeting has already ended.", 409)
        row = await self.ensure(meeting)
        # Duplicate pause after a split cannot create another empty session.
        if not row.is_started:
            return row
        row.end_ms = max(row.start_ms, boundary_ms, meeting.duration_ms or 0)
        next_row = MeetingSection(
            meeting_id=meeting.id,
            sequence=row.sequence + 1,
            title=f"Phiên {row.sequence + 1}",
            start_ms=row.end_ms,
            is_started=False,
        )
        self.session.add(next_row)
        await self.session.flush()
        return next_row

    async def finish(self, meeting: Meeting) -> None:
        row = await self.ensure(meeting)
        if row.end_ms is None:
            current_end = await self.session.scalar(
                select(func.max(CurrentSegment.end_ms))
                .join(
                    Transcript,
                    CurrentSegment.transcript_id == Transcript.id,
                )
                .where(Transcript.meeting_id == meeting.id)
            )
            if current_end is None and meeting.current_transcript_version_id:
                current_end = await self.session.scalar(
                    select(func.max(SegmentVersion.end_ms)).where(
                        SegmentVersion.transcript_version_id
                        == meeting.current_transcript_version_id
                    )
                )
            row.end_ms = max(row.start_ms, meeting.duration_ms or 0, current_end or 0)
        await self.session.flush()

    async def rename(
        self, meeting_id: UUID, section_id: UUID, actor: User, title: str
    ) -> MeetingSectionResponse:
        return await self.update(meeting_id, section_id, actor, SectionUpdateRequest(title=title))


def sections_revision(rows: list[MeetingSection]) -> str:
    if not rows:
        return ""
    return hashlib.sha256(
        json.dumps(
            [
                [str(row.id), row.sequence, row.title, row.start_ms, row.end_ms, row.is_started]
                for row in rows
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def check_sections_revision(rows: list[MeetingSection], expected: str | None) -> None:
    if expected is not None and expected != sections_revision(rows):
        raise ApiError(
            "SECTION_CONFLICT",
            "Danh sách phiên vừa thay đổi. Tải lại phiên và kiểm tra trước khi lưu.",
            409,
        )


def section_responses(rows: list[MeetingSection]) -> list[MeetingSectionResponse]:
    revision = sections_revision(rows)
    return [
        MeetingSectionResponse.model_validate(row).model_copy(update={"revision": revision})
        for row in rows
    ]


def group_segments(segments: list[dict], sections: list[dict]) -> list[dict]:
    """Half-open start-time assignment: each row belongs to exactly one session."""
    if not sections:
        sections = [{"id": None, "title": "Phiên 1", "sequence": 1, "start_ms": 0, "end_ms": None}]
    groups = [{**section, "segments": []} for section in sections]
    for segment in segments:
        candidates = [group for group in groups if group["start_ms"] <= segment["start_ms"]]
        target = candidates[-1] if candidates else groups[0]
        target["segments"].append(segment)
    return groups
