"""Provider-neutral translation and summary persistence plus durable AI jobs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.session import Database
from app.models.enums import JobStatus, MeetingPermission
from app.models.media import Job
from app.models.summary import Summary, SummaryVersion
from app.models.transcript import SegmentVersion, TranscriptVersion
from app.models.translation import Translation, TranslationSegment
from app.models.user import User
from app.providers.contracts import (
    LLMProvider,
    LLMProviderError,
    TranscriptionSegment,
    TranslationProvider,
    TranslationProviderError,
)
from app.providers.storage import MediaStorage
from app.schemas.ai import (
    SummaryCreateRequest,
    SummaryResponse,
    SummaryVersionResponse,
    TranslationCreateRequest,
    TranslationResponse,
    TranslationSegmentResponse,
)
from app.schemas.media import JobErrorResponse, JobResponse
from app.services.exports import ExportJobRunner
from app.services.meetings import MeetingService
from app.workers import lifecycle


class AIService:
    def __init__(self, session: AsyncSession, translation: TranslationProvider | None, llm: LLMProvider | None) -> None:
        self._session = session
        self._translation = translation
        self._llm = llm
        self._meetings = MeetingService(session)

    async def request_translation(self, meeting_id: UUID, user: User, payload: TranslationCreateRequest) -> JobResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.EDIT)
        if self._translation is None:
            raise ApiError("PROVIDER_CAPABILITY_UNAVAILABLE", "Translation is not configured.", 503)
        if (payload.source_language, payload.target_language) not in self._translation.capabilities().supported_language_pairs:
            raise ApiError("PROVIDER_CAPABILITY_UNAVAILABLE", "This translation language pair is not supported.", 422)
        version = await self._valid_current_transcript(meeting.current_transcript_version_id, meeting_id=meeting.id)
        job = Job(
            meeting_id=meeting.id,
            job_type="translation",
            status=JobStatus.QUEUED,
            progress=0,
            payload={"transcript_version_id": str(version.id), "source_language": payload.source_language, "target_language": payload.target_language, "actor_id": str(user.id)},
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return _job_response(job)

    async def request_summary(self, meeting_id: UUID, user: User, payload: SummaryCreateRequest) -> JobResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.EDIT)
        if self._llm is None or not self._llm.capabilities().structured_output:
            raise ApiError("PROVIDER_CAPABILITY_UNAVAILABLE", "AI summary generation is not configured.", 503)
        version = await self._valid_current_transcript(meeting.current_transcript_version_id, meeting_id=meeting.id)
        job = Job(
            meeting_id=meeting.id,
            job_type="summary",
            status=JobStatus.QUEUED,
            progress=0,
            payload={"transcript_version_id": str(version.id), "template": payload.template.value, "output_language": payload.output_language, "actor_id": str(user.id)},
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return _job_response(job)

    async def list_translations(self, meeting_id: UUID, user: User) -> list[TranslationResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        translations = (await self._session.scalars(
            select(Translation)
            .where(Translation.meeting_id == meeting_id)
            .options(selectinload(Translation.segments))
            .order_by(Translation.created_at.desc())
        )).all()
        return [self._translation_response_sync(item) for item in translations]

    async def translation(self, translation_id: UUID, user: User) -> TranslationResponse:
        translation = await self._session.get(Translation, translation_id)
        if translation is None:
            raise ApiError("TRANSLATION_NOT_FOUND", "Translation was not found.", 404)
        await self._meetings.require_access(translation.meeting_id, user, MeetingPermission.READ)
        return await self._translation_response(translation)

    async def list_summaries(self, meeting_id: UUID, user: User) -> list[SummaryResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        summaries = (await self._session.scalars(
            select(Summary)
            .where(Summary.meeting_id == meeting_id)
            .options(selectinload(Summary.versions))
            .order_by(Summary.created_at.desc())
        )).all()
        responses: list[SummaryResponse] = []
        for item in summaries:
            if not item.versions:
                continue
            latest = max(item.versions, key=lambda v: v.version_no)
            responses.append(
                SummaryResponse(
                    id=item.id,
                    meeting_id=item.meeting_id,
                    transcript_version_id=item.transcript_version_id,
                    template=item.template_type,
                    output_language=item.output_language,
                    current_version=self._summary_version_response(latest),
                )
            )
        return responses

    async def summary(self, summary_id: UUID, user: User) -> SummaryResponse:
        summary = await self._session.get(Summary, summary_id)
        if summary is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary was not found.", 404)
        await self._meetings.require_access(summary.meeting_id, user, MeetingPermission.READ)
        return await self._summary_response(summary)

    async def summary_versions(self, summary_id: UUID, user: User) -> list[SummaryVersionResponse]:
        summary = await self._summary_for_read(summary_id, user)
        versions = await self._session.scalars(select(SummaryVersion).where(SummaryVersion.summary_id == summary.id).order_by(SummaryVersion.version_no.desc()))
        return [self._summary_version_response(item) for item in versions]

    async def summary_version(self, summary_id: UUID, version_id: UUID, user: User) -> SummaryVersionResponse:
        summary = await self._summary_for_read(summary_id, user)
        version = await self._session.get(SummaryVersion, version_id)
        if version is None or version.summary_id != summary.id:
            raise ApiError("SUMMARY_VERSION_NOT_FOUND", "Summary version was not found.", 404)
        return self._summary_version_response(version)

    async def _valid_current_transcript(self, version_id: UUID | None, meeting_id: UUID | None = None) -> TranscriptVersion:
        if version_id is None and meeting_id is not None:
            from app.models.transcript import Transcript
            transcript = await self._session.scalar(select(Transcript).where(Transcript.meeting_id == meeting_id))
            if transcript:
                latest_v = await self._session.scalar(
                    select(TranscriptVersion)
                    .where(TranscriptVersion.transcript_id == transcript.id)
                    .order_by(TranscriptVersion.version_no.desc())
                    .limit(1)
                )
                if latest_v:
                    version_id = latest_v.id
        if version_id is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "A transcript is required for this operation.", 422)
        version = await self._session.get(TranscriptVersion, version_id)
        if version is None:
            raise ApiError("TRANSCRIPT_INVALID", "Transcript input is empty or invalid.", 422)
        segments = await self._segments(version.id)
        if not segments:
            raise ApiError("TRANSCRIPT_INVALID", "Transcript input is empty or invalid.", 422)
        return version

    def _translation_response_sync(self, translation: Translation) -> TranslationResponse:
        segments = sorted(translation.segments, key=lambda s: s.sequence) if translation.segments else []
        return TranslationResponse(
            id=translation.id,
            meeting_id=translation.meeting_id,
            transcript_version_id=translation.transcript_version_id,
            source_language=translation.source_language,
            target_language=translation.target_language,
            created_at=translation.created_at,
            segments=[
                TranslationSegmentResponse(logical_segment_id=item.logical_segment_id, sequence=item.sequence, text=item.text)
                for item in segments
            ],
        )

    async def _translation_response(self, translation: Translation) -> TranslationResponse:
        segments = await self._session.scalars(select(TranslationSegment).where(TranslationSegment.translation_id == translation.id).order_by(TranslationSegment.sequence))
        return TranslationResponse(id=translation.id, meeting_id=translation.meeting_id, transcript_version_id=translation.transcript_version_id, source_language=translation.source_language, target_language=translation.target_language, created_at=translation.created_at, segments=[TranslationSegmentResponse(logical_segment_id=item.logical_segment_id, sequence=item.sequence, text=item.text) for item in segments])

    async def _summary_response(self, summary: Summary) -> SummaryResponse:
        current = await self._session.scalar(select(SummaryVersion).where(SummaryVersion.summary_id == summary.id).order_by(SummaryVersion.version_no.desc()).limit(1))
        if current is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary is not complete.", 404)
        return SummaryResponse(id=summary.id, meeting_id=summary.meeting_id, transcript_version_id=summary.transcript_version_id, template=summary.template_type, output_language=summary.output_language, current_version=self._summary_version_response(current))

    async def _summary_for_read(self, summary_id: UUID, user: User) -> Summary:
        summary = await self._session.get(Summary, summary_id)
        if summary is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary was not found.", 404)
        await self._meetings.require_access(summary.meeting_id, user, MeetingPermission.READ)
        return summary

    async def _segments(self, version_id: UUID) -> list[SegmentVersion]:
        segments = list(await self._session.scalars(select(SegmentVersion).where(SegmentVersion.transcript_version_id == version_id).order_by(SegmentVersion.sequence)))
        if not segments:
            version = await self._session.get(TranscriptVersion, version_id)
            if version:
                from datetime import datetime, UTC
                from app.models.transcript import CurrentSegment
                rows = list(await self._session.scalars(
                    select(CurrentSegment)
                    .where(CurrentSegment.transcript_id == version.transcript_id)
                    .order_by(CurrentSegment.position, CurrentSegment.logical_segment_id)
                ))
                if rows:
                    for sequence, item in enumerate(rows, start=1):
                        self._session.add(SegmentVersion(
                            transcript_version_id=version.id,
                            logical_segment_id=item.logical_segment_id,
                            speaker_id=item.speaker_id,
                            sequence=sequence,
                            start_ms=item.start_ms,
                            end_ms=item.end_ms,
                            text=item.text,
                            original_stt_text=item.original_stt_text,
                            confidence=item.confidence,
                        ))
                    version.checkpointed_at = datetime.now(UTC)
                    await self._session.flush()
                    segments = list(await self._session.scalars(
                        select(SegmentVersion)
                        .where(SegmentVersion.transcript_version_id == version_id)
                        .order_by(SegmentVersion.sequence)
                    ))
        valid: list[SegmentVersion] = []
        for s in segments:
            if not s.text or not s.text.strip():
                continue
            if s.start_ms >= s.end_ms:
                s.end_ms = s.start_ms + 1000
            valid.append(s)
        return valid

    @staticmethod
    def _summary_version_response(version: SummaryVersion) -> SummaryVersionResponse:
        return SummaryVersionResponse(id=version.id, version_no=version.version_no, content=version.content, provider=version.provider, model=version.model, created_by=version.created_by, created_at=version.created_at)


class AIJobDispatcher:
    """Translation, summary, and export runner invoked by the queue worker."""

    def __init__(self, database: Database, settings: Settings, translation: TranslationProvider | None, llm: LLMProvider | None, storage: MediaStorage) -> None:
        self._database, self._settings, self._translation, self._llm = database, settings, translation, llm
        self._exports = ExportJobRunner(storage)

    async def run(self, job_id: UUID) -> bool:
        """Run a durable job and return whether the actor must retry it."""
        async with self._database.session() as session:
            job = await session.get(Job, job_id)
            if job is None or not await lifecycle.start(session, job):
                return False
            try:
                if job.job_type == "translation":
                    await self._run_translation(session, job)
                elif job.job_type == "summary":
                    await self._run_summary(session, job)
                elif job.job_type == "export":
                    await self._exports.run(session, job)
                else:
                    raise ValueError("Unsupported AI job type.")
                await session.commit()
                await lifecycle.completed(session, job)
                return False
            except (TranslationProviderError, LLMProviderError) as exc:
                return await self._fail(session, job, exc.code, str(exc), exc.retryable)
            except ValueError as exc:
                return await self._fail(session, job, "AI_RESULT_INVALID", str(exc), False)
            except Exception:
                code = "EXPORT_GENERATION_FAILED" if job.job_type == "export" else "AI_PROVIDER_ERROR"
                return await self._fail(session, job, code, "The job could not complete.", True)

    async def _run_translation(self, session: AsyncSession, job: Job) -> None:
        if self._translation is None:
            raise TranslationProviderError("Translation is not configured.", retryable=False)
        payload = job.payload or {}
        version_id, actor_id = UUID(str(payload["transcript_version_id"])), UUID(str(payload["actor_id"]))
        source, target = str(payload["source_language"]), str(payload["target_language"])
        if (source, target) not in self._translation.capabilities().supported_language_pairs:
            raise TranslationProviderError("This translation language pair is not supported.", code="PROVIDER_CAPABILITY_UNAVAILABLE", retryable=False)
        originals = await _valid_segments(session, version_id)
        translated = await self._translation.translate_segments(tuple(TranscriptionSegment(item.start_ms, item.end_ms, item.text) for item in originals), source, target)
        if len(translated) != len(originals) or any(not item.text.strip() for item in translated):
            raise ValueError("Translation provider returned invalid segment output.")
        translation = Translation(meeting_id=job.meeting_id, transcript_version_id=version_id, source_language=source, target_language=target)
        session.add(translation)
        await session.flush()
        session.add_all(TranslationSegment(translation_id=translation.id, logical_segment_id=original.logical_segment_id, sequence=index, text=result.text.strip()) for index, (original, result) in enumerate(zip(originals, translated, strict=True), start=1))
        job.payload = {**payload, "translation_id": str(translation.id), "actor_id": str(actor_id)}

    async def _run_summary(self, session: AsyncSession, job: Job) -> None:
        if self._llm is None or not self._llm.capabilities().structured_output:
            raise LLMProviderError("AI summary generation is not configured.", code="PROVIDER_CAPABILITY_UNAVAILABLE", retryable=False)
        payload = job.payload or {}
        version_id, actor_id = UUID(str(payload["transcript_version_id"])), UUID(str(payload["actor_id"]))
        template = str(payload["template"])
        output_language = str(payload["output_language"])
        originals = await _valid_segments(session, version_id)
        content = _normalize_summary(await self._llm.generate_summary("\n".join(item.text for item in originals), template, output_language))
        summary = await session.scalar(select(Summary).where(Summary.meeting_id == job.meeting_id, Summary.transcript_version_id == version_id, Summary.template_type == template, Summary.output_language == output_language))
        if summary is None:
            from app.models.enums import SummaryTemplateType
            summary = Summary(meeting_id=job.meeting_id, transcript_version_id=version_id, template_type=SummaryTemplateType(template), output_language=output_language)
            session.add(summary)
            await session.flush()
        version_no = int(await session.scalar(select(func.coalesce(func.max(SummaryVersion.version_no), 0) + 1).where(SummaryVersion.summary_id == summary.id)))
        session.add(SummaryVersion(summary_id=summary.id, version_no=version_no, content=content, provider=self._settings.llm_provider or self._llm.__class__.__name__, model=self._settings.llm_model or "configured-default", created_by=actor_id))
        job.payload = {**payload, "summary_id": str(summary.id)}

    async def _fail(self, session: AsyncSession, job: Job, code: str, message: str, retryable: bool) -> bool:
        if retryable and job.attempts <= self._settings.worker_max_retries:
            await lifecycle.retrying(
                session,
                job,
                code,
                message,
                min_backoff_ms=self._settings.worker_retry_min_backoff_ms,
                max_backoff_ms=self._settings.worker_retry_max_backoff_ms,
            )
            return True
        await lifecycle.failed(session, job, code, message, retryable)
        return False


async def _valid_segments(session: AsyncSession, version_id: UUID) -> list[SegmentVersion]:
    segments = list(await session.scalars(select(SegmentVersion).where(SegmentVersion.transcript_version_id == version_id).order_by(SegmentVersion.sequence)))
    if not segments:
        version = await session.get(TranscriptVersion, version_id)
        if version:
            from datetime import datetime, UTC
            from app.models.transcript import CurrentSegment
            rows = list(await session.scalars(
                select(CurrentSegment)
                .where(CurrentSegment.transcript_id == version.transcript_id)
                .order_by(CurrentSegment.position, CurrentSegment.logical_segment_id)
            ))
            if rows:
                for sequence, item in enumerate(rows, start=1):
                    session.add(SegmentVersion(
                        transcript_version_id=version.id,
                        logical_segment_id=item.logical_segment_id,
                        speaker_id=item.speaker_id,
                        sequence=sequence,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
                        text=item.text,
                        original_stt_text=item.original_stt_text,
                        confidence=item.confidence,
                    ))
                version.checkpointed_at = datetime.now(UTC)
                await session.flush()
                segments = list(await session.scalars(
                    select(SegmentVersion)
                    .where(SegmentVersion.transcript_version_id == version_id)
                    .order_by(SegmentVersion.sequence)
                ))
    valid: list[SegmentVersion] = []
    for s in segments:
        if not s.text or not s.text.strip():
            continue
        if s.start_ms >= s.end_ms:
            s.end_ms = s.start_ms + 1000
        valid.append(s)
    if not valid:
        raise ValueError("Transcript input is empty or invalid.")
    return valid


def _normalize_summary(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("overview"), str) or not value["overview"].strip():
        raise ValueError("Summary provider returned invalid structured output.")
    result: dict[str, object] = {"overview": value["overview"].strip()}
    for key in ("key_points", "action_items", "decisions", "risks_notes"):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Summary provider returned invalid structured output.")
        result[key] = [item.strip() for item in items]
    return result


def _job_response(job: Job) -> JobResponse:
    error = JobErrorResponse(code=job.error_code, message=job.error_message, retryable=job.error_retryable) if job.error_code and job.error_message else None
    return JobResponse(id=job.id, meeting_id=job.meeting_id, media_file_id=job.media_file_id, job_type=job.job_type, status=job.status, progress=job.progress, error=error, created_at=job.created_at, updated_at=job.updated_at)
