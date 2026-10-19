"""Transcript snapshots, segment editing, and meeting speaker management."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.enums import MeetingPermission, TranscriptVersionSource
from app.models.media import MediaFile
from app.models.meeting import Meeting
from app.models.transcript import (
    CurrentSegment,
    SegmentVersion,
    Speaker,
    Transcript,
    TranscriptEditEvent,
    TranscriptVersion,
)
from app.models.user import User
from app.providers.contracts import TranscriptionSegment
from app.schemas.transcript import (
    SegmentEditRequest,
    SegmentMergeRequest,
    SegmentResponse,
    SegmentSpeakerRequest,
    SegmentSplitRequest,
    SpeakerCreateRequest,
    SpeakerResponse,
    SpeakerSampleResponse,
    SpeakerUpdateRequest,
    TranscriptMutationResponse,
    TranscriptResponse,
    TranscriptVersionResponse,
)
from app.services.meetings import MeetingService
from app.services.transcript_versions import TranscriptVersionReader


class TranscriptService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._meetings = MeetingService(session)

    async def current(
        self, meeting_id: UUID, user: User, *, limit: int = 100, offset: int = 0,
        cursor: str | None = None,
    ) -> TranscriptResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        version = await self._current_version(meeting)
        transcript = await self._transcript_for_meeting(meeting.id)
        if transcript.current_projection_initialized_at is not None:
            return await self._current_projection_response(meeting, transcript, version, limit, cursor)
        return await self._transcript_response_async(meeting, version, limit=limit, offset=offset)

    async def current_for_shared_link(self, meeting: Meeting) -> TranscriptResponse:
        """Return only the current transcript after a share token has been validated."""
        version = await self._current_version(meeting)
        transcript = await self._transcript_for_meeting(meeting.id)
        if transcript.current_projection_initialized_at is not None:
            return await self._current_projection_response(meeting, transcript, version, 500, None)
        return await self._transcript_response_async(meeting, version, limit=500, offset=0)

    async def versions(
        self, meeting_id: UUID, user: User, *, limit: int = 100, offset: int = 0
    ) -> list[TranscriptVersionResponse]:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        transcript = await self._session.scalar(select(Transcript).where(Transcript.meeting_id == meeting.id))
        if transcript is None:
            return []
        versions = await self._session.scalars(
            select(TranscriptVersion)
            .where(TranscriptVersion.transcript_id == transcript.id)
            .order_by(TranscriptVersion.version_no.desc())
            .offset(offset)
            .limit(limit)
        )
        return [self._version_response(item) for item in versions]

    async def version(
        self, meeting_id: UUID, version_id: UUID, user: User, *, limit: int = 100, offset: int = 0
    ) -> TranscriptResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        version = await self._session.get(TranscriptVersion, version_id)
        if version is None or version.transcript_id != await self._transcript_id(meeting.id):
            raise ApiError("TRANSCRIPT_VERSION_NOT_FOUND", "Transcript version was not found.", 404)
        transcript = await self._transcript_for_meeting(meeting.id)
        if transcript.current_projection_initialized_at is None:
            return await self._transcript_response_async(meeting, version, limit=limit, offset=offset)
        return await self._historical_response(meeting, version, limit=limit, offset=offset)

    async def edit_segment(
        self, logical_id: UUID, user: User, payload: SegmentEditRequest
    ) -> TranscriptMutationResponse:
        return await self._projected_edit(logical_id, user, payload)

    async def split_segment(
        self, logical_id: UUID, user: User, payload: SegmentSplitRequest
    ) -> TranscriptMutationResponse:
        return await self._projected_split(logical_id, user, payload)

    async def delete_segment(self, logical_id: UUID, user: User) -> TranscriptMutationResponse:
        return await self._projected_delete(logical_id, user)

    async def assign_speaker(
        self, logical_id: UUID, user: User, payload: SegmentSpeakerRequest
    ) -> TranscriptMutationResponse:
        meeting, _, _ = await self._projection_context(logical_id, user)
        if payload.speaker_id is not None:
            speaker = await self._session.get(Speaker, payload.speaker_id)
            if speaker is None or speaker.meeting_id != meeting.id:
                raise ApiError("SPEAKER_NOT_FOUND", "Speaker was not found for this meeting.", 404)
        return await self._projected_speaker(logical_id, user, payload.speaker_id)

    async def merge_segments(
        self, payload: SegmentMergeRequest, user: User
    ) -> TranscriptMutationResponse:
        ids = payload.segment_ids or ([payload.first_segment_id, payload.second_segment_id] if payload.first_segment_id and payload.second_segment_id else [])
        if not ids or len(ids) < 2:
            raise ApiError("SEGMENTS_NOT_MERGEABLE", "Cần ít nhất hai đoạn để gộp.", 422)

        meeting, first, transcript = await self._projection_context(ids[0], user)
        other_ids = [seg_id for seg_id in ids if seg_id != first.logical_segment_id]
        others = list(
            await self._session.scalars(
                select(CurrentSegment)
                .where(
                    CurrentSegment.transcript_id == transcript.id,
                    CurrentSegment.logical_segment_id.in_(other_ids),
                )
                .order_by(CurrentSegment.position)
                .with_for_update()
            )
        )
        if len(others) != len(set(other_ids)):
            raise ApiError("SEGMENTS_NOT_MERGEABLE", "Một hoặc nhiều đoạn không tồn tại trong transcript.", 422)

        all_selected = [first] + others
        all_selected.sort(key=lambda s: s.position)

        min_pos = all_selected[0].position
        max_pos = all_selected[-1].position
        count_in_between = await self._session.scalar(
            select(func.count(CurrentSegment.id)).where(
                CurrentSegment.transcript_id == transcript.id,
                CurrentSegment.position >= min_pos,
                CurrentSegment.position <= max_pos,
            )
        )
        if count_in_between != len(all_selected):
            raise ApiError("SEGMENTS_NOT_MERGEABLE", "Các đoạn cần gộp phải nằm liền kề nhau trong transcript.", 422)

        version = await self._new_user_version(meeting, transcript, user)
        primary = all_selected[0]
        primary.end_ms = max(s.end_ms for s in all_selected)
        merged_text = " ".join(s.text.strip() for s in all_selected if s.text.strip())
        primary.text = merged_text

        del_ids = [seg.id for seg in all_selected[1:]]
        await self._session.execute(delete(CurrentSegment).where(CurrentSegment.id.in_(del_ids)))

        await self._event(
            transcript,
            version,
            user.id,
            "segments_merged",
            {
                "first_logical_segment_id": str(all_selected[0].logical_segment_id),
                "second_logical_segment_id": str(all_selected[1].logical_segment_id),
                "merged_segment_ids": [str(s.logical_segment_id) for s in all_selected],
                "end_ms": primary.end_ms,
                "text": primary.text,
            },
        )
        return await self._finish_mutation(meeting, transcript, version, primary)

    async def list_speakers(self, meeting_id: UUID, user: User) -> list[SpeakerResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        speakers = await self._session.scalars(
            select(Speaker).where(Speaker.meeting_id == meeting_id).order_by(Speaker.created_at, Speaker.id)
        )
        return [SpeakerResponse.model_validate(speaker) for speaker in speakers]

    async def speaker_sample(self, speaker_id: UUID, user: User) -> SpeakerSampleResponse:
        speaker = await self._session.get(Speaker, speaker_id)
        if speaker is None:
            raise ApiError("SPEAKER_NOT_FOUND", "Speaker was not found.", 404)
        await self._meetings.require_access(speaker.meeting_id, user, MeetingPermission.READ)
        media = await self._session.scalar(
            select(MediaFile).where(
                MediaFile.meeting_id == speaker.meeting_id,
                MediaFile.deleted_at.is_(None),
            ).order_by(MediaFile.created_at.desc()).limit(1)
        )
        if media is None:
            raise ApiError("SPEAKER_SAMPLE_UNAVAILABLE", "No playable media is available for this speaker.", 404)
        transcript = await self._session.scalar(select(Transcript).where(Transcript.meeting_id == speaker.meeting_id))
        if transcript is None:
            raise ApiError("SPEAKER_SAMPLE_UNAVAILABLE", "No transcript sample is available for this speaker.", 404)
        if transcript.current_projection_initialized_at is not None:
            segment = await self._session.scalar(
                select(CurrentSegment).where(
                    CurrentSegment.transcript_id == transcript.id, CurrentSegment.speaker_id == speaker.id
                ).order_by((CurrentSegment.end_ms - CurrentSegment.start_ms).desc()).limit(1)
            )
        else:
            meeting = await self._session.get(Meeting, speaker.meeting_id)
            if meeting is None:
                raise ApiError("SPEAKER_SAMPLE_UNAVAILABLE", "No meeting is available for this speaker.", 404)
            version = await self._current_version(meeting)
            segment = await self._session.scalar(
                select(SegmentVersion).where(
                    SegmentVersion.transcript_version_id == version.id, SegmentVersion.speaker_id == speaker.id
                ).order_by(SegmentVersion.sequence).limit(1)
            )
        if segment is None:
            raise ApiError("SPEAKER_SAMPLE_UNAVAILABLE", "No transcript sample is available for this speaker.", 404)
        return SpeakerSampleResponse(media_id=media.id, start_ms=segment.start_ms, end_ms=segment.end_ms)

    async def create_speaker(
        self, meeting_id: UUID, user: User, payload: SpeakerCreateRequest
    ) -> SpeakerResponse:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.EDIT)
        speaker = Speaker(
            meeting_id=meeting_id,
            name=payload.name.strip(),
            role=payload.role,
            organization=payload.organization,
            voice_profile_id=payload.voice_profile_id,
            is_identified=bool(payload.voice_profile_id),
        )
        self._session.add(speaker)
        await self._session.commit()
        await self._session.refresh(speaker)
        return SpeakerResponse.model_validate(speaker)

    async def update_speaker(
        self, speaker_id: UUID, user: User, payload: SpeakerUpdateRequest
    ) -> SpeakerResponse:
        speaker = await self._session.get(Speaker, speaker_id)
        if speaker is None:
            raise ApiError("SPEAKER_NOT_FOUND", "Speaker was not found.", 404)
        await self._meetings.require_access(speaker.meeting_id, user, MeetingPermission.EDIT)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(speaker, field, value.strip() if field == "name" and value is not None else value)
        if payload.voice_profile_id is not None:
            speaker.is_identified = True
        await self._session.commit()
        await self._session.refresh(speaker)
        return SpeakerResponse.model_validate(speaker)

    async def delete_speaker(self, speaker_id: UUID, user: User) -> None:
        speaker = await self._session.get(Speaker, speaker_id)
        if speaker is None:
            raise ApiError("SPEAKER_NOT_FOUND", "Speaker was not found.", 404)
        await self._meetings.require_access(speaker.meeting_id, user, MeetingPermission.EDIT)
        await self._session.execute(
            update(CurrentSegment).where(CurrentSegment.speaker_id == speaker_id).values(speaker_id=None)
        )
        await self._session.execute(
            update(SegmentVersion).where(SegmentVersion.speaker_id == speaker_id).values(speaker_id=None)
        )
        await self._session.delete(speaker)
        await self._session.commit()

    async def append_stt_final(
        self, meeting: Meeting, actor_id: UUID, segment: TranscriptionSegment,
        *, start_new_version: bool = False,
        candidate_name: str | None = None,
        voice_profile_id: UUID | None = None,
        match_confidence: float | None = None,
    ) -> SegmentVersion:
        """Shared ingestion boundary for realtime and future upload/batch STT."""
        if not segment.text.strip() or segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
            raise ValueError("Final transcript result is invalid.")
        await self._session.refresh(meeting, with_for_update=True)
        transcript = await self._ensure_transcript(meeting.id)
        version = await self._session.scalar(
            select(TranscriptVersion)
            .where(TranscriptVersion.transcript_id == transcript.id, TranscriptVersion.source == TranscriptVersionSource.STT)
            .order_by(TranscriptVersion.version_no.desc()).limit(1)
        )
        if start_new_version or version is None or meeting.current_transcript_version_id != version.id:
            previous = await self._current_version_or_none(meeting)
            version = TranscriptVersion(
                transcript_id=transcript.id,
                version_no=await self._next_version_no(transcript.id),
                source=TranscriptVersionSource.STT,
                parent_version_id=previous.id if previous else None,
                created_by=actor_id,
            )
            self._session.add(version)
            await self._session.flush()
            meeting.current_transcript_version_id = version.id
            if transcript.current_projection_initialized_at is not None:
                # A revision may precede this turn. Start the new STT snapshot
                # with the full projection, including its corrected speakers.
                await self._checkpoint(transcript, version)
        speaker_id = await self._speaker_for_label(
            meeting.id,
            segment.speaker_label,
            candidate_name=segment.speaker_name or candidate_name,
            voice_profile_id=segment.voice_profile_id or voice_profile_id,
            match_confidence=match_confidence,
        )
        sequence = int(await self._session.scalar(select(func.coalesce(func.max(SegmentVersion.sequence), 0) + 1).where(SegmentVersion.transcript_version_id == version.id)))
        stored = SegmentVersion(
            transcript_version_id=version.id, speaker_id=speaker_id, sequence=sequence,
            start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text.strip(),
            original_stt_text=segment.text.strip(), confidence=segment.confidence,
        )
        self._session.add(stored)
        await self._session.flush()
        if transcript.current_projection_initialized_at is None:
            for sequence, item in enumerate(await self._segments(version.id), start=1):
                self._session.add(CurrentSegment(transcript_id=transcript.id, logical_segment_id=item.logical_segment_id, speaker_id=item.speaker_id, position=Decimal(sequence * 1024), start_ms=item.start_ms, end_ms=item.end_ms, text=item.text, original_stt_text=item.original_stt_text, confidence=item.confidence))
            transcript.current_projection_initialized_at = datetime.now(UTC)
            version.checkpointed_at = datetime.now(UTC)
        else:
            position = await self._session.scalar(select(func.coalesce(func.max(CurrentSegment.position), 0) + 1024).where(CurrentSegment.transcript_id == transcript.id))
            self._session.add(CurrentSegment(transcript_id=transcript.id, logical_segment_id=stored.logical_segment_id, speaker_id=stored.speaker_id, position=position, start_ms=stored.start_ms, end_ms=stored.end_ms, text=stored.text, original_stt_text=stored.original_stt_text, confidence=stored.confidence))
        await self._session.commit()
        await self._session.refresh(stored)
        return stored

    async def replace_stt_batch(
        self, meeting: Meeting, actor_id: UUID, segments: tuple[TranscriptionSegment, ...]
    ) -> None:
        """Persist a complete batch response as one immutable STT snapshot.

        Unlike realtime finals, an upload must not leave a partially committed
        transcript when one malformed provider segment is encountered.
        """
        if not segments or any(
            not item.text.strip() or item.start_ms < 0 or item.end_ms <= item.start_ms
            for item in segments
        ):
            raise ValueError("Batch transcript result is invalid.")
        if any(right.start_ms < left.end_ms for left, right in zip(segments, segments[1:], strict=False)):
            raise ValueError("Batch transcript segments overlap.")
        transcript = await self._ensure_transcript(meeting.id)
        previous = await self._current_version_or_none(meeting)
        version = TranscriptVersion(
            transcript_id=transcript.id,
            version_no=await self._next_version_no(transcript.id),
            source=TranscriptVersionSource.STT,
            parent_version_id=previous.id if previous else None,
            created_by=actor_id,
        )
        self._session.add(version)
        await self._session.flush()
        for sequence, segment in enumerate(segments, start=1):
            self._session.add(
                SegmentVersion(
                    transcript_version_id=version.id,
                    speaker_id=await self._speaker_for_label(
                        meeting.id,
                        segment.speaker_label,
                        candidate_name=segment.speaker_name,
                        voice_profile_id=segment.voice_profile_id,
                    ),
                    sequence=sequence,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text.strip(),
                    original_stt_text=segment.text.strip(),
                    confidence=segment.confidence,
                )
            )
        meeting.current_transcript_version_id = version.id
        await self._session.flush()
        await self._session.execute(
            delete(CurrentSegment).where(CurrentSegment.transcript_id == transcript.id)
        )
        for sequence, segment in enumerate(await self._segments(version.id), start=1):
            self._session.add(CurrentSegment(transcript_id=transcript.id, logical_segment_id=segment.logical_segment_id, speaker_id=segment.speaker_id, position=Decimal(sequence * 1024), start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text, original_stt_text=segment.original_stt_text, confidence=segment.confidence))
        version.checkpointed_at = datetime.now(UTC)
        transcript.current_projection_initialized_at = datetime.now(UTC)

    async def rollback(
        self, meeting_id: UUID, target_version_id: UUID, user: User
    ) -> TranscriptMutationResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.EDIT)
        transcript = await self._transcript_for_meeting(meeting.id)
        self._require_projection(transcript)
        meeting = await self._session.scalar(select(Meeting).where(Meeting.id == meeting.id).with_for_update())
        assert meeting is not None
        target = await self._session.get(TranscriptVersion, target_version_id)
        if target is None or target.transcript_id != transcript.id:
            raise ApiError("TRANSCRIPT_VERSION_NOT_FOUND", "Transcript version was not found.", 404)
        state = await self._reconstruct(target)
        version = await self._new_user_version(meeting, transcript, user)
        await self._session.execute(
            delete(CurrentSegment).where(CurrentSegment.transcript_id == transcript.id)
        )
        for item in state:
            self._session.add(CurrentSegment(transcript_id=transcript.id, logical_segment_id=item["logical_segment_id"], speaker_id=item["speaker_id"], position=item["position"], start_ms=item["start_ms"], end_ms=item["end_ms"], text=item["text"], original_stt_text=item.get("original_stt_text"), confidence=item.get("confidence")))
        await self._session.flush()
        await self._event(transcript, version, user.id, "rollback", {"target_version_id": str(target.id)})
        await self._checkpoint(transcript, version)
        meeting.current_transcript_version_id = version.id
        await self._session.commit()
        return TranscriptMutationResponse(version_id=version.id, version_no=version.version_no)

    async def _projected_edit(
        self, logical_id: UUID, user: User, payload: SegmentEditRequest
    ) -> TranscriptMutationResponse:
        meeting, segment, transcript = await self._projection_context(logical_id, user)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(segment, field, value.strip() if field == "text" and isinstance(value, str) else value)
        await self._validate_current_neighbors(transcript.id, segment)
        version = await self._new_user_version(meeting, transcript, user)
        await self._event(transcript, version, user.id, "segment_edited", {"logical_segment_id": str(segment.logical_segment_id), **{key: getattr(segment, key) for key in payload.model_fields_set}})
        return await self._finish_mutation(meeting, transcript, version, segment)

    async def _projected_speaker(
        self, logical_id: UUID, user: User, speaker_id: UUID | None
    ) -> TranscriptMutationResponse:
        meeting, segment, transcript = await self._projection_context(logical_id, user)
        version = await self._new_user_version(meeting, transcript, user)
        segment.speaker_id = speaker_id
        await self._event(transcript, version, user.id, "speaker_assigned", {"logical_segment_id": str(segment.logical_segment_id), "speaker_id": str(speaker_id) if speaker_id else None})
        return await self._finish_mutation(meeting, transcript, version, segment)

    async def revise_stt_speaker(
        self, logical_id: UUID, user: User, label: str | None
    ) -> TranscriptMutationResponse | None:
        """Record provider corrections as revisions without overwriting user choices."""
        segment = await self._session.scalar(
            select(CurrentSegment).where(CurrentSegment.logical_segment_id == logical_id)
            .execution_options(populate_existing=True)
        )
        if segment is None:
            return None  # Deleted/merged while recording.
        meeting, segment, transcript = await self._projection_context(logical_id, user)
        await self._session.refresh(meeting)
        edits = await self._session.scalars(
            select(TranscriptEditEvent).where(TranscriptEditEvent.transcript_id == transcript.id)
        )
        for edit in edits:
            if edit.payload.get("source") == "provider":
                continue
            if edit.event_type in {"speaker_assigned", "segment_split", "segments_merged"}:
                if str(logical_id) in {
                    str(edit.payload.get(key)) for key in (
                        "logical_segment_id", "first_logical_segment_id",
                        "second_logical_segment_id", "new_logical_segment_id",
                    )
                }:
                    return None
        speaker_id = await self._speaker_for_label(meeting.id, label)
        if segment.speaker_id == speaker_id:
            return None
        version = await self._new_user_version(meeting, transcript, user)
        segment.speaker_id = speaker_id
        await self._event(transcript, version, user.id, "speaker_assigned", {
            "logical_segment_id": str(logical_id),
            "speaker_id": str(speaker_id) if speaker_id else None,
            "source": "provider",
        })
        return await self._finish_mutation(meeting, transcript, version, segment)

    async def _projected_delete(self, logical_id: UUID, user: User) -> TranscriptMutationResponse:
        meeting, segment, transcript = await self._projection_context(logical_id, user)
        version = await self._new_user_version(meeting, transcript, user)
        await self._session.delete(segment)
        await self._event(transcript, version, user.id, "segment_deleted", {"logical_segment_id": str(logical_id)})
        return await self._finish_mutation(meeting, transcript, version, None)

    async def _projected_split(
        self, logical_id: UUID, user: User, payload: SegmentSplitRequest
    ) -> TranscriptMutationResponse:
        meeting, segment, transcript = await self._projection_context(logical_id, user)
        if not segment.start_ms < payload.split_at_ms < segment.end_ms:
            raise ApiError("INVALID_SEGMENT_TIMESTAMPS", "split_at_ms must fall inside the segment.", 422)
        words = segment.text.split()
        midpoint = max(1, len(words) // 2)
        first_text = payload.first_text.strip() if payload.first_text else " ".join(words[:midpoint])
        second_text = payload.second_text.strip() if payload.second_text else " ".join(words[midpoint:])
        if not first_text or not second_text:
            raise ApiError("INVALID_SEGMENT_SPLIT", "Both split segments require text.", 422)
        position = await self._next_position(transcript.id, segment)
        old_end = segment.end_ms
        segment.end_ms, segment.text = payload.split_at_ms, first_text
        created = CurrentSegment(transcript_id=transcript.id, logical_segment_id=uuid4(), speaker_id=segment.speaker_id, position=position, start_ms=payload.split_at_ms, end_ms=old_end, text=second_text, original_stt_text=segment.original_stt_text, confidence=segment.confidence)
        self._session.add(created)
        version = await self._new_user_version(meeting, transcript, user)
        await self._event(transcript, version, user.id, "segment_split", {"logical_segment_id": str(segment.logical_segment_id), "new_logical_segment_id": str(created.logical_segment_id), "split_at_ms": payload.split_at_ms, "first_text": first_text, "second_text": second_text, "new_position": str(position)})
        return await self._finish_mutation(meeting, transcript, version, segment)

    async def _projection_context(
        self, logical_id: UUID, user: User
    ) -> tuple[Meeting, CurrentSegment, Transcript]:
        segment = await self._session.scalar(select(CurrentSegment).where(CurrentSegment.logical_segment_id == logical_id).with_for_update())
        if segment is None:
            legacy = await self._session.scalar(
                select(Transcript)
                .join(TranscriptVersion, TranscriptVersion.transcript_id == Transcript.id)
                .join(Meeting, Meeting.current_transcript_version_id == TranscriptVersion.id)
                .join(SegmentVersion, SegmentVersion.transcript_version_id == TranscriptVersion.id)
                .where(SegmentVersion.logical_segment_id == logical_id)
            )
            if legacy is not None:
                meeting = await self._session.get(Meeting, legacy.meeting_id)
                assert meeting is not None
                await self._meetings.require_meeting_access(meeting, user, MeetingPermission.EDIT)
                self._require_projection(legacy)
            raise ApiError("SEGMENT_NOT_FOUND", "Transcript segment was not found.", 404)
        transcript = await self._session.get(Transcript, segment.transcript_id)
        assert transcript is not None
        meeting = await self._session.scalar(select(Meeting).where(Meeting.id == transcript.meeting_id).with_for_update())
        assert meeting is not None
        await self._meetings.require_meeting_access(meeting, user, MeetingPermission.EDIT)
        self._require_projection(transcript)
        return meeting, segment, transcript

    @staticmethod
    def _require_projection(transcript: Transcript) -> None:
        if transcript.current_projection_initialized_at is None:
            raise ApiError("TRANSCRIPT_PROJECTION_UNAVAILABLE", "Transcript editing is unavailable until projection migration completes.", 409)

    async def _new_user_version(self, meeting: Meeting, transcript: Transcript, user: User) -> TranscriptVersion:
        parent = await self._current_version(meeting)
        next_v = (parent.version_no + 1) if (parent and parent.version_no is not None) else await self._next_version_no(transcript.id)
        version = TranscriptVersion(transcript_id=transcript.id, version_no=next_v, source=TranscriptVersionSource.USER_EDIT, parent_version_id=parent.id, created_by=user.id)
        self._session.add(version)
        await self._session.flush()
        return version

    async def _event(self, transcript: Transcript, version: TranscriptVersion, actor_id: UUID, event_type: str, payload: dict[str, object]) -> None:
        self._session.add(TranscriptEditEvent(transcript_id=transcript.id, version_id=version.id, actor_id=actor_id, event_order=1, event_type=event_type, payload=payload))

    async def _finish_mutation(self, meeting: Meeting, transcript: Transcript, version: TranscriptVersion, segment: CurrentSegment | None) -> TranscriptMutationResponse:
        event_count = int(await self._session.scalar(select(func.count(TranscriptEditEvent.id)).where(TranscriptEditEvent.transcript_id == transcript.id)) or 0)
        if event_count % 25 == 0:
            await self._checkpoint(transcript, version)
        meeting.current_transcript_version_id = version.id
        await self._session.commit()
        return TranscriptMutationResponse(version_id=version.id, version_no=version.version_no, segment=self._current_segment_response(segment) if segment else None)

    async def _checkpoint(self, transcript: Transcript, version: TranscriptVersion) -> None:
        rows = await self._session.scalars(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript.id).order_by(CurrentSegment.position, CurrentSegment.logical_segment_id))
        for sequence, item in enumerate(rows, start=1):
            self._session.add(SegmentVersion(transcript_version_id=version.id, logical_segment_id=item.logical_segment_id, speaker_id=item.speaker_id, sequence=sequence, start_ms=item.start_ms, end_ms=item.end_ms, text=item.text, original_stt_text=item.original_stt_text, confidence=item.confidence))
        version.checkpointed_at = datetime.now(UTC)
        await self._session.flush()

    async def _next_position(self, transcript_id: UUID, segment: CurrentSegment) -> Decimal:
        following = await self._session.scalar(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript_id, CurrentSegment.position > segment.position).order_by(CurrentSegment.position).limit(1).with_for_update())
        position = (segment.position + following.position) / 2 if following else segment.position + Decimal(1024)
        if following is not None and following.position - segment.position <= Decimal("0.000000000000001"):
            await self._compact_positions(transcript_id)
            await self._session.refresh(segment)
            following = await self._session.scalar(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript_id, CurrentSegment.position > segment.position).order_by(CurrentSegment.position).limit(1).with_for_update())
            position = (segment.position + following.position) / 2 if following else segment.position + Decimal(1024)
        return position

    async def _compact_positions(self, transcript_id: UUID) -> None:
        rows = await self._session.scalars(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript_id).order_by(CurrentSegment.position, CurrentSegment.logical_segment_id).with_for_update())
        for index, row in enumerate(rows, start=1):
            row.position = Decimal(index * 1024)
        await self._session.flush()

    async def _are_adjacent(self, first: CurrentSegment, second: CurrentSegment) -> bool:
        following = await self._session.scalar(select(CurrentSegment.logical_segment_id).where(CurrentSegment.transcript_id == first.transcript_id, CurrentSegment.position > first.position).order_by(CurrentSegment.position, CurrentSegment.logical_segment_id).limit(1))
        return following == second.logical_segment_id

    async def _validate_current_neighbors(self, transcript_id: UUID, segment: CurrentSegment) -> None:
        if segment.start_ms < 0 or segment.start_ms >= segment.end_ms or not segment.text.strip():
            raise ApiError("INVALID_SEGMENT_TIMESTAMPS", "Segment values are invalid.", 422)
        previous = await self._session.scalar(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript_id, CurrentSegment.position < segment.position).order_by(CurrentSegment.position.desc()).limit(1))
        following = await self._session.scalar(select(CurrentSegment).where(CurrentSegment.transcript_id == transcript_id, CurrentSegment.position > segment.position).order_by(CurrentSegment.position).limit(1))
        if (previous and segment.start_ms < previous.end_ms) or (following and segment.end_ms > following.start_ms):
            raise ApiError("INVALID_SEGMENT_ORDER", "Transcript segments must be chronologically ordered without overlap.", 422)

    async def _mutate_segment(
        self, logical_id: UUID, user: User, operation: Callable[[list[dict[str, object]], int], object]
    ) -> TranscriptMutationResponse:
        meeting, _, version = await self._current_segment_with_meeting(logical_id, user)
        await self._meetings.require_meeting_access(meeting, user, MeetingPermission.EDIT)
        items = [self._copy_data(item) for item in await self._segments(version.id)]
        index = next(index for index, item in enumerate(items) if item["logical_segment_id"] == logical_id)
        operation(items, index)
        return await self._save_snapshot(meeting, version, user.id, items, index if index < len(items) else None)

    async def _save_snapshot(self, meeting: Meeting, parent: TranscriptVersion, actor_id: UUID, items: list[dict[str, object]], changed_index: int | None) -> TranscriptMutationResponse:
        self._validate_segments(items)
        next_v = (parent.version_no + 1) if (parent and parent.version_no is not None) else await self._next_version_no(parent.transcript_id)
        next_version = TranscriptVersion(
            transcript_id=parent.transcript_id, version_no=next_v,
            source=TranscriptVersionSource.USER_EDIT, parent_version_id=parent.id, created_by=actor_id,
        )
        self._session.add(next_version)
        await self._session.flush()
        stored: list[SegmentVersion] = []
        for sequence, item in enumerate(items, start=1):
            segment = SegmentVersion(transcript_version_id=next_version.id, sequence=sequence, **item)
            self._session.add(segment)
            stored.append(segment)
        meeting.current_transcript_version_id = next_version.id
        await self._session.commit()
        response_segment = None
        if changed_index is not None and changed_index < len(stored):
            response_segment = self._segment_response(stored[changed_index])
        return TranscriptMutationResponse(version_id=next_version.id, version_no=next_version.version_no, segment=response_segment)

    async def _current_segment_with_meeting(self, logical_id: UUID, user: User) -> tuple[Meeting, SegmentVersion, TranscriptVersion]:
        segment = await self._session.scalar(
            select(SegmentVersion)
            .join(TranscriptVersion, SegmentVersion.transcript_version_id == TranscriptVersion.id)
            .join(Transcript, TranscriptVersion.transcript_id == Transcript.id)
            .join(Meeting, Transcript.meeting_id == Meeting.id)
            .where(
                SegmentVersion.logical_segment_id == logical_id,
                Meeting.current_transcript_version_id == TranscriptVersion.id,
            )
        )
        if segment is None:
            raise ApiError("SEGMENT_NOT_FOUND", "Transcript segment was not found.", 404)
        version = await self._session.get(TranscriptVersion, segment.transcript_version_id)
        assert version is not None
        transcript = await self._session.get(Transcript, version.transcript_id)
        assert transcript is not None
        meeting = await self._session.get(Meeting, transcript.meeting_id)
        assert meeting is not None
        await self._meetings.require_meeting_access(meeting, user, MeetingPermission.READ)
        return meeting, segment, version

    async def _current_version(self, meeting: Meeting) -> TranscriptVersion:
        version = await self._current_version_or_none(meeting)
        if version is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "No transcript exists for this meeting.", 404)
        return version

    async def _current_version_or_none(self, meeting: Meeting) -> TranscriptVersion | None:
        return await self._session.get(TranscriptVersion, meeting.current_transcript_version_id) if meeting.current_transcript_version_id else None

    async def _transcript_id(self, meeting_id: UUID) -> UUID | None:
        return await self._session.scalar(select(Transcript.id).where(Transcript.meeting_id == meeting_id))

    async def _ensure_transcript(self, meeting_id: UUID) -> Transcript:
        transcript = await self._session.scalar(select(Transcript).where(Transcript.meeting_id == meeting_id))
        if transcript is None:
            transcript = Transcript(meeting_id=meeting_id)
            self._session.add(transcript)
            await self._session.flush()
        return transcript

    async def _next_version_no(self, transcript_id: UUID) -> int:
        latest = await self._session.scalar(select(func.max(TranscriptVersion.version_no)).where(TranscriptVersion.transcript_id == transcript_id))
        return int(latest or 0) + 1

    async def _segments(
        self, version_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> list[SegmentVersion]:
        query = (
            select(SegmentVersion)
            .where(SegmentVersion.transcript_version_id == version_id)
            .order_by(SegmentVersion.sequence)
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        return list(await self._session.scalars(query))

    async def _transcript_for_meeting(self, meeting_id: UUID) -> Transcript:
        transcript = await self._session.scalar(select(Transcript).where(Transcript.meeting_id == meeting_id))
        if transcript is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "No transcript exists for this meeting.", 404)
        return transcript

    async def _current_projection_response(
        self, meeting: Meeting, transcript: Transcript, version: TranscriptVersion, limit: int,
        cursor: str | None,
    ) -> TranscriptResponse:
        query = select(CurrentSegment).where(CurrentSegment.transcript_id == transcript.id)
        if cursor:
            position, logical_id = self._parse_cursor(cursor)
            query = query.where(or_(CurrentSegment.position > position, and_(CurrentSegment.position == position, CurrentSegment.logical_segment_id > logical_id)))
        rows = list(await self._session.scalars(query.order_by(CurrentSegment.position, CurrentSegment.logical_segment_id).limit(limit + 1)))
        page, extra = rows[:limit], len(rows) > limit
        next_cursor = None
        if extra and page:
            last = page[-1]
            next_cursor = f"{last.position}:{last.logical_segment_id}"
        return TranscriptResponse(version_id=version.id, version_no=version.version_no, language=meeting.language_mode, segments=[self._current_segment_response(item) for item in page], next_cursor=next_cursor)

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[Decimal, UUID]:
        try:
            position, logical_id = cursor.rsplit(":", 1)
            return Decimal(position), UUID(logical_id)
        except (InvalidOperation, ValueError) as exc:
            raise ApiError("INVALID_TRANSCRIPT_CURSOR", "Transcript cursor is invalid.", 422) from exc

    async def _historical_response(
        self, meeting: Meeting, version: TranscriptVersion, *, limit: int, offset: int
    ) -> TranscriptResponse:
        rows = await self._reconstruct(version)
        page = rows[offset : offset + limit]
        return TranscriptResponse(version_id=version.id, version_no=version.version_no, language=meeting.language_mode, segments=[SegmentResponse(id=item["logical_segment_id"], logical_segment_id=item["logical_segment_id"], start_ms=item["start_ms"], end_ms=item["end_ms"], text=item["text"], speaker_id=item["speaker_id"], confidence=item.get("confidence")) for item in page])

    async def _reconstruct(self, version: TranscriptVersion) -> list[dict[str, object]]:
        return await TranscriptVersionReader(self._session).read(version)

    @staticmethod
    def _current_segment_response(segment: CurrentSegment) -> SegmentResponse:
        return SegmentResponse(id=segment.logical_segment_id, logical_segment_id=segment.logical_segment_id, start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text, speaker_id=segment.speaker_id, confidence=segment.confidence)

    async def _speaker_for_label(
        self,
        meeting_id: UUID,
        label: str | None,
        candidate_name: str | None = None,
        voice_profile_id: UUID | None = None,
        match_confidence: float | None = None,
    ) -> UUID | None:
        if voice_profile_id is not None:
            identified = await self._session.scalar(
                select(Speaker).where(
                    Speaker.meeting_id == meeting_id,
                    Speaker.voice_profile_id == voice_profile_id,
                )
            )
            if identified is not None:
                return identified.id
        if not label:
            if candidate_name:
                speaker = await self._session.scalar(
                    select(Speaker).where(
                        Speaker.meeting_id == meeting_id,
                        Speaker.voice_profile_id == voice_profile_id,
                    )
                )
                if speaker is None:
                    speaker = Speaker(
                        meeting_id=meeting_id,
                        name=candidate_name,
                        voice_profile_id=voice_profile_id,
                        match_confidence=match_confidence,
                        is_identified=True,
                    )
                    self._session.add(speaker)
                    await self._session.flush()
                return speaker.id
            return None

        speaker = await self._session.scalar(
            select(Speaker).where(
                Speaker.meeting_id == meeting_id,
                Speaker.provider_speaker_id == label,
            )
        )
        if speaker is None:
            if candidate_name:
                name = candidate_name
                is_identified = True
            else:
                count = int(
                    await self._session.scalar(
                        select(func.count(Speaker.id)).where(Speaker.meeting_id == meeting_id)
                    )
                    or 0
                )
                name = f"Người nói {count + 1}"
                is_identified = False

            speaker = Speaker(
                meeting_id=meeting_id,
                provider_speaker_id=label,
                name=name,
                voice_profile_id=voice_profile_id,
                match_confidence=match_confidence,
                is_identified=is_identified,
            )
            self._session.add(speaker)
            await self._session.flush()
        elif candidate_name and not speaker.is_identified:
            speaker.name = candidate_name
            speaker.voice_profile_id = voice_profile_id
            speaker.match_confidence = match_confidence
            speaker.is_identified = True
            await self._session.flush()

        return speaker.id

    async def _transcript_response_async(
        self, meeting: Meeting, version: TranscriptVersion, *, limit: int, offset: int
    ) -> TranscriptResponse:
        return TranscriptResponse(version_id=version.id, version_no=version.version_no, language=meeting.language_mode, segments=[self._segment_response(item) for item in await self._segments(version.id, limit=limit, offset=offset)])

    @staticmethod
    def _version_response(version: TranscriptVersion) -> TranscriptVersionResponse:
        return TranscriptVersionResponse(id=version.id, version_no=version.version_no, source=version.source, parent_version_id=version.parent_version_id, created_by=version.created_by, created_at=version.created_at)

    @staticmethod
    def _segment_response(segment: SegmentVersion) -> SegmentResponse:
        return SegmentResponse(id=segment.logical_segment_id, logical_segment_id=segment.logical_segment_id, start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text, speaker_id=segment.speaker_id, confidence=segment.confidence)

    @staticmethod
    def _copy_data(segment: SegmentVersion) -> dict[str, object]:
        return {"logical_segment_id": segment.logical_segment_id, "speaker_id": segment.speaker_id, "start_ms": segment.start_ms, "end_ms": segment.end_ms, "text": segment.text, "original_stt_text": segment.original_stt_text, "confidence": segment.confidence}

    @staticmethod
    def _validate_segments(items: list[dict[str, object]]) -> None:
        last_end = 0
        for item in items:
            start, end = int(item["start_ms"]), int(item["end_ms"])
            if start < 0 or start >= end:
                raise ApiError("INVALID_SEGMENT_TIMESTAMPS", "Each segment must have start_ms earlier than end_ms.", 422)
            if not str(item["text"]).strip():
                raise ApiError("INVALID_SEGMENT_TEXT", "Transcript segment text must not be empty.", 422)
            if start < last_end:
                raise ApiError("INVALID_SEGMENT_ORDER", "Transcript segments must be chronologically ordered without overlap.", 422)
            last_end = end

    @staticmethod
    def _edit(items: list[dict[str, object]], index: int, payload: SegmentEditRequest) -> None:
        item = items[index]
        for field, value in payload.model_dump(exclude_unset=True).items():
            item[field] = value.strip() if field == "text" and isinstance(value, str) else value

    @staticmethod
    def _set_speaker(items: list[dict[str, object]], index: int, speaker_id: UUID | None) -> None:
        items[index]["speaker_id"] = speaker_id

    @staticmethod
    def _split(items: list[dict[str, object]], index: int, payload: SegmentSplitRequest) -> None:
        item = items[index]
        start, end = int(item["start_ms"]), int(item["end_ms"])
        if not start < payload.split_at_ms < end:
            raise ApiError("INVALID_SEGMENT_TIMESTAMPS", "split_at_ms must fall inside the segment.", 422)
        words = str(item["text"]).split()
        midpoint = max(1, len(words) // 2)
        first_text = payload.first_text.strip() if payload.first_text else " ".join(words[:midpoint])
        second_text = payload.second_text.strip() if payload.second_text else " ".join(words[midpoint:])
        if not first_text or not second_text:
            raise ApiError("INVALID_SEGMENT_SPLIT", "Both split segments require text.", 422)
        item["end_ms"] = payload.split_at_ms
        item["text"] = first_text
        second = dict(item)
        second["logical_segment_id"] = uuid4()
        second["start_ms"] = payload.split_at_ms
        second["end_ms"] = end
        second["text"] = second_text
        items.insert(index + 1, second)
