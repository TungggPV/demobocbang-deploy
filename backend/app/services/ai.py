"""Provider-neutral translation and summary persistence plus durable AI jobs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.session import Database
from app.models.enums import JobStatus, MeetingPermission, SummaryTemplateType
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
    SummaryEditRequest,
    SummaryResponse,
    SummaryVersionResponse,
    TranslationCreateRequest,
    TranslationResponse,
    TranslationSegmentResponse,
)
from app.schemas.media import JobErrorResponse, JobResponse
from app.schemas.meeting_section import MeetingSectionResponse
from app.services.exports import ExportJobRunner
from app.services.meeting_sections import (
    MeetingSectionService,
    check_sections_revision,
    group_segments,
)
from app.services.meetings import MeetingService
from app.services.transcript_versions import TranscriptVersionReader
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
        version = await self._valid_current_transcript(meeting.current_transcript_version_id)
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
        await self._session.refresh(meeting, with_for_update=True)
        version = await self._session.get(TranscriptVersion, meeting.current_transcript_version_id) if meeting.current_transcript_version_id else None
        if version is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "A transcript is required for this operation.", 422)
        rows = await TranscriptVersionReader(self._session).read(version)
        valid_rows = []
        for row in rows:
            t = (row.get("text") or "").strip()
            if not t:
                continue
            s_ms = max(0, int(row.get("start_ms") or 0))
            e_ms = max(s_ms + 500, int(row.get("end_ms") or (s_ms + 500)))
            valid_rows.append({"text": t, "start_ms": s_ms, "end_ms": e_ms})
        if not valid_rows:
            raise ApiError("TRANSCRIPT_INVALID", "Transcript input is empty or invalid.", 422)
        inputs = valid_rows
        section_rows = await MeetingSectionService(self._session).rows(meeting_id)
        check_sections_revision(section_rows, payload.sections_revision)
        sections = [MeetingSectionResponse.model_validate(row).model_dump(mode="json", exclude={"revision"})
                    for row in section_rows]
        session_inputs = group_segments(inputs, sections) if payload.scope == "sessions" else None
        if payload.section_ids is not None:
            selected = {str(item) for item in payload.section_ids}
            if not selected.issubset({section["id"] for section in sections}):
                raise ApiError("MEETING_SECTION_NOT_FOUND", "Một phiên đã chọn không còn tồn tại hoặc không thuộc cuộc họp này.", 422)
            # Group against ALL boundaries first; filtering beforehand would steal other sessions' text.
            session_inputs = [section for section in session_inputs if section["id"] in selected]
        job = Job(
            meeting_id=meeting.id,
            job_type="summary",
            status=JobStatus.QUEUED,
            progress=0,
            payload={
                "scope": payload.scope,
                "input_segments": inputs if payload.scope == "meeting" else None,
                "session_inputs": session_inputs,
                "section_ids": [section["id"] for section in session_inputs] if session_inputs else None,
                "transcript_version_id": str(version.id),
                "mode": payload.mode,
                "template": (
                    payload.template.value
                    if payload.mode == "template" and payload.template
                    else None
                ),
                "prompt": payload.prompt if payload.mode == "custom" else None,
                "output_language": payload.output_language,
                "actor_id": str(user.id),
            },
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return _job_response(job)

    async def list_translations(self, meeting_id: UUID, user: User) -> list[TranslationResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        translations = await self._session.scalars(select(Translation).where(Translation.meeting_id == meeting_id).order_by(Translation.created_at.desc()))
        return [await self._translation_response(item) for item in translations]

    async def translation(self, translation_id: UUID, user: User) -> TranslationResponse:
        translation = await self._session.get(Translation, translation_id)
        if translation is None:
            raise ApiError("TRANSLATION_NOT_FOUND", "Translation was not found.", 404)
        await self._meetings.require_access(translation.meeting_id, user, MeetingPermission.READ)
        return await self._translation_response(translation)

    async def list_summaries(self, meeting_id: UUID, user: User) -> list[SummaryResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        summaries = await self._session.scalars(select(Summary).where(Summary.meeting_id == meeting_id).order_by(Summary.created_at.desc()))
        return [await self._summary_response(item) for item in summaries]

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

    async def edit_summary(
        self,
        summary_id: UUID,
        user: User,
        payload: SummaryEditRequest,
    ) -> SummaryResponse:
        summary = await self._session.get(Summary, summary_id)
        if summary is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary was not found.", 404)
        await self._meetings.require_access(
            summary.meeting_id, user, MeetingPermission.EDIT
        )
        await self._session.refresh(summary, with_for_update=True)
        current = await self._session.scalar(
            select(SummaryVersion)
            .where(SummaryVersion.summary_id == summary.id)
            .order_by(SummaryVersion.version_no.desc())
            .limit(1)
        )
        if current is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary is not complete.", 404)
        if payload.expected_version_no is not None and payload.expected_version_no != current.version_no:
            raise ApiError("VERSION_CONFLICT", "This summary was changed by another editor. Reload first.", 409)
        if summary.scope == "sessions":
            old, edited = current.content.get("sessions"), payload.content.get("sessions")
            if not isinstance(old, list) or not isinstance(edited, list) or len(old) != len(edited):
                raise ApiError("SUMMARY_INVALID", "Keep every meeting session when editing.", 422)
            for before, after in zip(old, edited, strict=True):
                if not isinstance(after, dict) or {key: value for key, value in before.items() if key != "content"} != {key: value for key, value in after.items() if key != "content"}:
                    raise ApiError("SUMMARY_INVALID", "Session metadata cannot be changed in a summary.", 422)
                if (before.get("content") is None and after.get("content") is not None) or (before.get("content") is not None and not isinstance(after.get("content"), dict)):
                    raise ApiError("SUMMARY_INVALID", "Session summary content must retain its object shape.", 422)
        next_version = SummaryVersion(
            summary_id=summary.id,
            version_no=current.version_no + 1,
            prompt=current.prompt,
            content=payload.content,
            provider="user",
            model="manual-edit",
            created_by=user.id,
        )
        self._session.add(next_version)
        await self._session.commit()
        return await self._summary_response(summary)

    async def _valid_current_transcript(self, version_id: UUID | None) -> TranscriptVersion:
        if version_id is None:
            raise ApiError("TRANSCRIPT_NOT_FOUND", "A transcript is required for this operation.", 422)
        version = await self._session.get(TranscriptVersion, version_id)
        if version is None or not await self._segments(version.id):
            raise ApiError("TRANSCRIPT_INVALID", "Transcript input is empty or invalid.", 422)
        return version

    async def _translation_response(self, translation: Translation) -> TranslationResponse:
        segments = await self._session.scalars(select(TranslationSegment).where(TranslationSegment.translation_id == translation.id).order_by(TranslationSegment.sequence))
        return TranslationResponse(id=translation.id, meeting_id=translation.meeting_id, transcript_version_id=translation.transcript_version_id, source_language=translation.source_language, target_language=translation.target_language, created_at=translation.created_at, segments=[TranslationSegmentResponse(logical_segment_id=item.logical_segment_id, sequence=item.sequence, text=item.text) for item in segments])

    async def _summary_response(self, summary: Summary) -> SummaryResponse:
        current = await self._session.scalar(select(SummaryVersion).where(SummaryVersion.summary_id == summary.id).order_by(SummaryVersion.version_no.desc()).limit(1))
        if current is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary is not complete.", 404)
        return SummaryResponse(
            id=summary.id,
            meeting_id=summary.meeting_id,
            transcript_version_id=summary.transcript_version_id,
            scope=summary.scope,
            mode=summary.mode,
            template=summary.template_type if summary.mode == "template" else None,
            output_language=summary.output_language,
            current_version=self._summary_version_response(current),
        )

    async def _summary_for_read(self, summary_id: UUID, user: User) -> Summary:
        summary = await self._session.get(Summary, summary_id)
        if summary is None:
            raise ApiError("SUMMARY_NOT_FOUND", "Summary was not found.", 404)
        await self._meetings.require_access(summary.meeting_id, user, MeetingPermission.READ)
        return summary

    async def _segments(self, version_id: UUID) -> list[SegmentVersion]:
        segments = list(await self._session.scalars(select(SegmentVersion).where(SegmentVersion.transcript_version_id == version_id).order_by(SegmentVersion.sequence)))
        if any(not item.text.strip() or item.start_ms >= item.end_ms for item in segments):
            return []
        return segments

    @staticmethod
    def _summary_version_response(version: SummaryVersion) -> SummaryVersionResponse:
        return SummaryVersionResponse(id=version.id, version_no=version.version_no, prompt=version.prompt, content=version.content, provider=version.provider, model=version.model, created_by=version.created_by, created_at=version.created_at)


class AIJobDispatcher:
    """Translation, summary, and export runner invoked by the queue worker."""

    def __init__(self, database: Database, settings: Settings, translation: TranslationProvider | None, llm: LLMProvider | None, storage: MediaStorage) -> None:
        self._database, self._settings, self._translation, self._llm = database, settings, translation, llm
        self._exports = ExportJobRunner(storage)

    async def run(self, job_id: UUID) -> bool:
        """Run a durable job and return whether the actor must retry it."""
        async with self._database.session() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None or not await lifecycle.start(session, job):
                return False
            try:
                if job.job_type == "translation":
                    await self._run_translation(session, job)
                elif job.job_type == "summary":
                    await self._run_summary(session, job)
                elif job.job_type == "transcript_polish":
                    from app.services.transcript_polish import run_polish
                    await run_polish(session, job, self._llm)
                elif job.job_type == "export":
                    await self._exports.run(session, job)
                else:
                    raise ValueError("Unsupported AI job type.")
                if job.job_type != "transcript_polish":
                    await session.commit()
                await lifecycle.completed(session, job)
                return False
            except (TranslationProviderError, LLMProviderError) as exc:
                return await self._fail(session, job, exc.code, str(exc), exc.retryable)
            except ValueError as exc:
                return await self._fail(session, job, "AI_RESULT_INVALID", str(exc), False)
            except ApiError as exc:
                return await self._fail(session, job, exc.code, exc.message, False)
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
        mode = str(payload["mode"])
        template_value = payload.get("template")
        template = str(template_value) if template_value else None
        user_prompt = str(payload["prompt"]) if payload.get("prompt") else None
        output_language = str(payload["output_language"])
        scope = str(payload.get("scope", "meeting"))
        input_rows = payload.get("input_segments")
        if input_rows is None and scope != "sessions":  # Jobs queued before the session feature.
            input_rows = [{"text": row.text} for row in await _valid_segments(session, version_id)]
        template_type = (
            SummaryTemplateType(template)
            if mode == "template" and template
            else SummaryTemplateType.GENERAL
        )
        async def summarize(rows):
            return _normalize_summary(await self._llm.generate_summary(
                "\n".join(row["text"] for row in rows), user_prompt, output_language, template,
            ), mode)
        if scope == "sessions":
            outputs = []
            for index, section in enumerate(payload["session_inputs"], 1):
                outputs.append({**{key: value for key, value in section.items() if key != "segments"},
                                "content": await summarize(section["segments"]) if section["segments"] else None})
                job.progress = min(90, 10 + 80 * index // len(payload["session_inputs"]))
                await session.commit()  # Progress only; no partial summary is published.
            content = {"sessions": outputs}
        else:
            content = await summarize(input_rows)
        summary = await session.scalar(
            select(Summary).where(
                Summary.meeting_id == job.meeting_id,
                Summary.transcript_version_id == version_id,
                Summary.scope == scope,
                Summary.mode == mode,
                Summary.template_type == template_type,
                Summary.output_language == output_language,
            ).with_for_update()
        )
        if summary is None:
            summary = Summary(
                meeting_id=job.meeting_id,
                transcript_version_id=version_id,
                scope=scope,
                mode=mode,
                template_type=template_type,
                output_language=output_language,
            )
            session.add(summary)
            await session.flush()
        version_no = int(await session.scalar(select(func.coalesce(func.max(SummaryVersion.version_no), 0) + 1).where(SummaryVersion.summary_id == summary.id)))
        stored_prompt = (
            user_prompt
            if mode == "custom" and user_prompt
            else _template_prompt(template_type)
        )
        session.add(SummaryVersion(summary_id=summary.id, version_no=version_no, prompt=stored_prompt, content=content, provider=self._settings.llm_provider or self._llm.__class__.__name__, model=self._settings.llm_model or "configured-default", created_by=actor_id))
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
                        end_ms=max(item.end_ms, item.start_ms + 500),
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
            s.end_ms = s.start_ms + 500
        valid.append(s)
    if not valid:
        raise ValueError("Transcript input is empty or invalid.")
    return valid


def _normalize_summary(value: dict[str, object], mode: str) -> dict[str, object]:
    if mode == "custom":
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Summary provider returned invalid custom output.")
        return {"content": content.strip()}
    if not isinstance(value, dict) or not isinstance(value.get("overview"), str) or not value["overview"].strip():
        raise ValueError("Summary provider returned invalid structured output.")
    result: dict[str, object] = {"overview": value["overview"].strip()}
    for key in ("key_points", "action_items", "decisions", "risks_notes"):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Summary provider returned invalid structured output.")
        result[key] = [item.strip() for item in items]
    return result


def _template_prompt(template: SummaryTemplateType) -> str:
    return {
        SummaryTemplateType.GENERAL: "Tóm tắt chung nội dung cuộc họp.",
        SummaryTemplateType.BRIEFING: "Tóm tắt cuộc họp giao ban.",
        SummaryTemplateType.VOTING: "Tóm tắt cuộc họp biểu quyết.",
    }[template]


def _job_response(job: Job) -> JobResponse:
    error = JobErrorResponse(code=job.error_code, message=job.error_message, retryable=job.error_retryable) if job.error_code and job.error_message else None
    return JobResponse(id=job.id, meeting_id=job.meeting_id, media_file_id=job.media_file_id, job_type=job.job_type, status=job.status, progress=job.progress, error=error, created_at=job.created_at, updated_at=job.updated_at)
