"""Read immutable transcript revisions from snapshots and edit events.

Callers enforce meeting access before using this internal reader.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.transcript import (
    SegmentVersion,
    Transcript,
    TranscriptEditEvent,
    TranscriptVersion,
)


class TranscriptVersionReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _segments(self, version_id: UUID) -> list[SegmentVersion]:
        return list(
            await self._session.scalars(
                select(SegmentVersion)
                .where(SegmentVersion.transcript_version_id == version_id)
                .order_by(SegmentVersion.sequence)
            )
        )

    async def read(self, version: TranscriptVersion) -> list[dict[str, object]]:
        transcript = await self._session.get(Transcript, version.transcript_id)
        if transcript is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "Transcript no longer exists.", 404)
        if transcript.current_projection_initialized_at is None:
            return [
                self._snapshot_data(item, Decimal(index * 1024))
                for index, item in enumerate(await self._segments(version.id), start=1)
            ]
        lineage: list[TranscriptVersion] = []
        visited: set[UUID] = set()
        current: TranscriptVersion | None = version
        while current is not None:
            if current.id in visited or current.transcript_id != version.transcript_id:
                raise ApiError(
                    "TRANSCRIPT_HISTORY_UNAVAILABLE", "Transcript version history is invalid.", 409
                )
            visited.add(current.id)
            lineage.append(current)
            if current.checkpointed_at is not None:
                break
            current = (
                await self._session.get(TranscriptVersion, current.parent_version_id)
                if current.parent_version_id
                else None
            )
        if not lineage or lineage[-1].checkpointed_at is None:
            raise ApiError(
                "TRANSCRIPT_HISTORY_UNAVAILABLE", "Transcript version has no valid checkpoint.", 409
            )
        checkpoint = lineage[-1]
        snapshots = await self._segments(checkpoint.id)
        state = [
            self._snapshot_data(item, Decimal(index * 1024))
            for index, item in enumerate(snapshots, start=1)
        ]
        by_id = {item["logical_segment_id"]: item for item in state}
        for revision in reversed(lineage[:-1]):
            events = await self._session.scalars(
                select(TranscriptEditEvent)
                .where(TranscriptEditEvent.version_id == revision.id)
                .order_by(TranscriptEditEvent.event_order)
            )
            for event in events:
                self._apply_event(state, by_id, event)
        return sorted(state, key=lambda item: (item["position"], item["logical_segment_id"]))

    @staticmethod
    def _snapshot_data(segment: SegmentVersion, position: Decimal) -> dict[str, object]:
        return {
            "logical_segment_id": segment.logical_segment_id,
            "speaker_id": segment.speaker_id,
            "position": position,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "original_stt_text": segment.original_stt_text,
            "confidence": segment.confidence,
        }

    @staticmethod
    def _apply_event(
        state: list[dict[str, object]],
        by_id: dict[UUID, dict[str, object]],
        event: TranscriptEditEvent,
    ) -> None:
        payload = event.payload
        if event.event_type == "segment_edited":
            item = by_id[UUID(str(payload["logical_segment_id"]))]
            item.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key in {"text", "start_ms", "end_ms"}
                }
            )
        elif event.event_type == "speaker_assigned":
            by_id[UUID(str(payload["logical_segment_id"]))]["speaker_id"] = (
                UUID(str(payload["speaker_id"])) if payload.get("speaker_id") else None
            )
        elif event.event_type == "segment_deleted":
            item = by_id.pop(UUID(str(payload["logical_segment_id"])))
            state.remove(item)
        elif event.event_type == "segment_split":
            item = by_id[UUID(str(payload["logical_segment_id"]))]
            original_end = item["end_ms"]
            item["end_ms"], item["text"] = payload["split_at_ms"], payload["first_text"]
            created = dict(
                item,
                logical_segment_id=UUID(str(payload["new_logical_segment_id"])),
                position=Decimal(str(payload["new_position"])),
                start_ms=payload["split_at_ms"],
                end_ms=original_end,
                text=payload["second_text"],
            )
            state.append(created)
            by_id[created["logical_segment_id"]] = created
        elif event.event_type == "segments_merged":
            first = by_id[UUID(str(payload["first_logical_segment_id"]))]
            merged_ids = payload.get("merged_segment_ids")
            if merged_ids and isinstance(merged_ids, list) and len(merged_ids) > 1:
                for mid in merged_ids[1:]:
                    uid = UUID(str(mid))
                    if uid in by_id:
                        sec = by_id.pop(uid)
                        if sec in state:
                            state.remove(sec)
            elif "second_logical_segment_id" in payload:
                second = by_id.pop(UUID(str(payload["second_logical_segment_id"])))
                if second in state:
                    state.remove(second)
            first["end_ms"], first["text"] = payload["end_ms"], payload["text"]
