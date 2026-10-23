"""Persist normalized final realtime-STT results without persisting partials."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MeetingStatus
from app.models.meeting import Meeting
from app.models.transcript import CurrentSegment, Transcript
from app.models.user import User
from app.providers.contracts import TranscriptionSegment
from app.schemas.transcript import TranscriptMutationResponse
from app.services.transcripts import TranscriptService


class RealtimeTranscriptService:
    """Owns durable final-segment writes for one realtime socket session."""

    def __init__(self, session: AsyncSession, meeting: Meeting, actor_id: UUID) -> None:
        self._session = session
        self._meeting = meeting
        self._actor_id = actor_id
        self._diarization_enabled = meeting.diarization_enabled
        self._speaker_scope = uuid4().hex
        self._turns: dict[int, UUID] = {}
        self._time_offset_ms = meeting.duration_ms or 0
        self._started_stt_version = False

    def _scoped_label(self, label: str | None) -> str | None:
        if not self._diarization_enabled or not label:
            return None
        return f"realtime:{self._speaker_scope}:{label}"

    @property
    def diarization_enabled(self) -> bool:
        return self._diarization_enabled

    @property
    def time_offset_ms(self) -> int:
        return self._time_offset_ms

    @property
    def stream_session_id(self) -> str:
        return self._speaker_scope

    async def _ensure_meeting(self) -> Meeting:
        if self._meeting not in self._session:
            refreshed = await self._session.get(Meeting, self._meeting.id)
            if refreshed is not None:
                self._meeting = refreshed
        return self._meeting

    async def recording_started(self) -> None:
        meeting = await self._ensure_meeting()
        if meeting.status in {
            MeetingStatus.CREATED,
            MeetingStatus.PROCESSING,
            MeetingStatus.FAILED,
        }:
            meeting.status = MeetingStatus.RECORDING
            await self._session.commit()

    async def recording_ended(self) -> None:
        meeting = await self._ensure_meeting()
        if meeting.status in {MeetingStatus.RECORDING, MeetingStatus.PROCESSING}:
            meeting.status = MeetingStatus.COMPLETED
            await self._session.commit()

    async def recording_failed(self) -> None:
        meeting = await self._ensure_meeting()
        meeting.status = MeetingStatus.FAILED
        await self._session.commit()

    async def persist_final(self, segment: TranscriptionSegment, turn_order: int | None = None):
        """Persist through the common STT ingestion boundary used by batch workers too."""
        meeting = await self._ensure_meeting()
        stored = await TranscriptService(self._session).append_stt_final(
            meeting, self._actor_id,
            replace(
                segment, speaker_label=self._scoped_label(segment.speaker_label),
                start_ms=segment.start_ms + self._time_offset_ms,
                end_ms=segment.end_ms + self._time_offset_ms,
            ),
            start_new_version=not self._started_stt_version,
        )
        self._started_stt_version = True
        meeting.duration_ms = max(meeting.duration_ms or 0, stored.end_ms)
        await self._session.commit()
        if turn_order is not None:
            self._turns[turn_order] = stored.logical_segment_id
        return stored

    async def revise_speaker(
        self, turn_order: int, label: str | None
    ) -> TranscriptMutationResponse | None:
        logical_id = self._turns.get(turn_order)
        if logical_id is None or not self._diarization_enabled:
            return None
        user = await self._session.get(User, self._actor_id)
        if user is None:
            return None
        mutation = await TranscriptService(self._session).revise_stt_speaker(
            logical_id, user, self._scoped_label(label)
        )
        # Even an unchanged/protected turn acquired the meeting lock. Do not
        # hold it while waiting for the next audio message on this socket.
        await self._session.commit()
        return mutation

    async def translation_inputs(
        self, logical_segment_ids: tuple[UUID, ...]
    ) -> tuple[tuple[UUID, TranscriptionSegment], ...]:
        """Load trusted current transcript text for client-requested realtime backfill."""
        if not logical_segment_ids:
            return ()
        rows = list(
            await self._session.scalars(
                select(CurrentSegment)
                .join(Transcript, CurrentSegment.transcript_id == Transcript.id)
                .where(
                    Transcript.meeting_id == self._meeting.id,
                    CurrentSegment.logical_segment_id.in_(logical_segment_ids),
                )
            )
        )
        by_id = {row.logical_segment_id: row for row in rows}
        if any(logical_id not in by_id for logical_id in logical_segment_ids):
            raise ValueError("A realtime translation segment was not found in this meeting.")
        return tuple(
            (
                logical_id,
                TranscriptionSegment(
                    start_ms=by_id[logical_id].start_ms,
                    end_ms=by_id[logical_id].end_ms,
                    text=by_id[logical_id].text,
                    confidence=by_id[logical_id].confidence,
                ),
            )
            for logical_id in logical_segment_ids
        )
