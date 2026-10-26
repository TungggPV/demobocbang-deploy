"""Durable proofreading proposals: generating never overwrites the transcript."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from app.core.errors import ApiError
from app.models.enums import JobStatus, MeetingPermission, MeetingStatus
from app.models.media import Job
from app.models.meeting import Meeting
from app.models.transcript import CurrentSegment, Transcript, TranscriptVersion
from app.models.user import User
from app.providers.contracts import LLMProvider, LLMProviderError
from app.schemas.meeting import MeetingResponse
from app.schemas.transcript_polish import PolishResponse, PolishReviewRequest
from app.services.ai import _job_response
from app.services.meetings import MeetingService
from app.services.transcript_versions import TranscriptVersionReader
from app.services.transcripts import TranscriptService


async def snapshot(session: AsyncSession, meeting: Meeting) -> list[dict]:
    version = (
        await session.get(TranscriptVersion, meeting.current_transcript_version_id)
        if meeting.current_transcript_version_id
        else None
    )
    if version is None:
        return []
    rows = await TranscriptVersionReader(session).read(version)
    return [
        {
            "logical_segment_id": str(row["logical_segment_id"]),
            "speaker_id": str(row["speaker_id"]) if row["speaker_id"] else None,
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "text": row["text"],
        }
        for row in rows
    ]


class TranscriptPolishService:
    def __init__(self, session: AsyncSession, llm: LLMProvider | None) -> None:
        self.session, self.llm = session, llm

    async def request(self, meeting_id: UUID, actor: User) -> Job:
        meeting, _ = await MeetingService(self.session).require_access(
            meeting_id, actor, MeetingPermission.EDIT
        )
        # Serialize requests per meeting; repeated completion/reload must not duplicate a proposal.
        await self.session.refresh(meeting, with_for_update=True)
        if meeting.status != MeetingStatus.COMPLETED:
            raise ApiError("MEETING_NOT_COMPLETED", "End the meeting before proofreading.", 409)
        if not self.llm or not callable(getattr(self.llm, "polish_transcript", None)):
            raise ApiError(
                "PROVIDER_CAPABILITY_UNAVAILABLE", "Transcript proofreading is not configured.", 503
            )
        originals = await snapshot(self.session, meeting)
        if not originals:
            raise ApiError(
                "TRANSCRIPT_INVALID", "There is no finalized transcript to proofread.", 422
            )
        jobs = await self.session.scalars(
            select(Job)
            .where(
                Job.meeting_id == meeting_id,
                Job.job_type == "transcript_polish",
            )
            .order_by(Job.created_at.desc())
        )
        for job in jobs:
            data = job.payload or {}
            if (
                data.get("source_version_id") == str(meeting.current_transcript_version_id)
                and data.get("originals") == originals
                and job.status != JobStatus.FAILED
            ):
                return job
        job = Job(
            meeting_id=meeting_id,
            job_type="transcript_polish",
            status=JobStatus.QUEUED,
            progress=0,
            created_at=datetime.now(UTC),
            payload={
                "source_version_id": str(meeting.current_transcript_version_id),
                "actor_id": str(actor.id),
                "originals": originals,
                "revision": 0,
                "prompt_version": "transcript-proofread-v1",
            },
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def latest(self, meeting_id: UUID, actor: User) -> PolishResponse | None:
        await MeetingService(self.session).require_access(meeting_id, actor, MeetingPermission.READ)
        job = await self.session.scalar(
            select(Job)
            .where(
                Job.meeting_id == meeting_id,
                Job.job_type == "transcript_polish",
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        return response(job) if job else None

    async def review(
        self,
        meeting_id: UUID,
        job_id: UUID,
        actor: User,
        body: PolishReviewRequest,
        *,
        accept: bool,
    ) -> PolishResponse:
        meeting, _ = await MeetingService(self.session).require_access(
            meeting_id, actor, MeetingPermission.EDIT
        )
        transcript = await self.session.scalar(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        )
        # Same lock order as manual segment edits and voice learning: segments, meeting, job.
        rows = (
            list(
                await self.session.scalars(
                    select(CurrentSegment)
                    .where(
                        CurrentSegment.transcript_id == transcript.id,
                    )
                    .order_by(CurrentSegment.logical_segment_id)
                    .with_for_update()
                )
            )
            if transcript
            else []
        )
        await self.session.refresh(meeting, with_for_update=True)
        job = await self.session.scalar(
            select(Job)
            .where(
                Job.id == job_id,
                Job.meeting_id == meeting_id,
                Job.job_type == "transcript_polish",
            )
            .with_for_update()
        )
        if job is None:
            raise ApiError("POLISH_NOT_FOUND", "Proofreading proposal was not found.", 404)
        data = job.payload or {}
        if data.get("accepted_version_id"):
            raise ApiError(
                "POLISH_ALREADY_ACCEPTED", "This proposal has already been finalized.", 409
            )
        if job.status != JobStatus.COMPLETED or not data.get("generated"):
            raise ApiError("POLISH_NOT_READY", "The AI proposal is not ready.", 409)
        if body.revision != data.get("revision", 0):
            raise ApiError(
                "VERSION_CONFLICT", "The draft was changed by another editor. Reload first.", 409
            )
        if (
            meeting.status != MeetingStatus.COMPLETED
            or str(meeting.current_transcript_version_id) != data["source_version_id"]
            or await snapshot(self.session, meeting) != data["originals"]
        ):
            raise ApiError(
                "VERSION_CONFLICT",
                "The source transcript changed. Generate a new proposal first.",
                409,
            )
        texts = {str(item.logical_segment_id): item.text.strip() for item in body.segments}
        if (
            len(texts) != len(body.segments)
            or set(texts) != {item["logical_segment_id"] for item in data["originals"]}
            or any(not text for text in texts.values())
        ):
            raise ApiError(
                "POLISH_INVALID", "Review must include exactly the original nonempty segments.", 422
            )
        data = {**data, "draft": texts, "revision": body.revision + 1}
        if accept:
            if transcript is None or len(rows) != len(texts):
                raise ApiError(
                    "TRANSCRIPT_PROJECTION_UNAVAILABLE", "Transcript editing is unavailable.", 409
                )
            version = await TranscriptService(self.session).accept_polished_text(
                meeting, transcript, rows, actor, texts, job.id
            )
            data.update(
                accepted_version_id=str(version.id),
                accepted_by=str(actor.id),
                accepted_at=datetime.now(UTC).isoformat(),
            )
        job.payload = data
        await self.session.commit()
        await self.session.refresh(job)
        return response(job)


def response(job: Job) -> PolishResponse:
    data = job.payload or {}
    generated, draft = data.get("generated", {}), data.get("draft", {})
    return PolishResponse(
        job=_job_response(job),
        source_version_id=data["source_version_id"],
        accepted_version_id=data.get("accepted_version_id"),
        revision=data.get("revision", 0),
        segments=[
            {
                **row,
                "original_text": row["text"],
                "generated_text": generated.get(row["logical_segment_id"], row["text"]),
                "text": draft.get(
                    row["logical_segment_id"], generated.get(row["logical_segment_id"], row["text"])
                ),
            }
            for row in data["originals"]
        ],
    )


async def run_polish(session: AsyncSession, job: Job, llm: LLMProvider | None) -> None:
    if not llm or not callable(getattr(llm, "polish_transcript", None)):
        raise LLMProviderError("Transcript proofreading is not configured.", retryable=False)
    data = job.payload or {}
    actor = await session.get(User, UUID(data["actor_id"]))
    if actor is None:
        raise ValueError("The requesting user no longer exists.")
    await MeetingService(session).require_access(job.meeting_id, actor, MeetingPermission.EDIT)
    originals = data["originals"]
    # Bound output size; long meetings use overlapping read-only context windows.
    batches: list[list[dict]] = []
    for row in originals:
        if (
            not batches
            or len(batches[-1]) >= 40
            or sum(len(item["text"]) for item in batches[-1]) + len(row["text"]) > 12000
        ):
            batches.append([])
        batches[-1].append(row)
    generated: dict[str, str] = {}
    for index, batch in enumerate(batches):
        context = "\n".join(item["text"] for item in (batches[index - 1] if index else []))[-3000:]
        if index + 1 < len(batches):
            context += "\n" + "\n".join(item["text"] for item in batches[index + 1])[:3000]
        result = await llm.polish_transcript(tuple(item["text"] for item in batch), context)
        if (
            not isinstance(result, (tuple, list))
            or len(result) != len(batch)
            or any(
                not isinstance(text, str) or not text.strip() or len(text) > 50000
                for text in result
            )
        ):
            raise ValueError("AI returned an invalid transcript alignment.")
        generated.update(
            {
                item["logical_segment_id"]: text.strip()
                for item, text in zip(batch, result, strict=True)
            }
        )
    actor_id = actor.id
    session.expire_all()
    await session.refresh(job)
    actor = await session.get(User, actor_id)
    if actor is None:
        raise ValueError("The requesting user no longer exists.")
    await MeetingService(session).require_access(job.meeting_id, actor, MeetingPermission.EDIT)
    job.payload = {
        **data,
        "generated": generated,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider_adapter": llm.__class__.__name__,
    }


async def complete_with_polish(
    session: AsyncSession,
    meeting_id: UUID,
    actor: User,
    app_state: State,
) -> MeetingResponse:
    """Commit completion and its proposal job together, then publish best-effort."""
    service = MeetingService(session)
    meeting, permission = await service.require_access(meeting_id, actor, MeetingPermission.EDIT)
    await session.refresh(meeting, with_for_update=True)
    was_completed = meeting.status == MeetingStatus.COMPLETED
    meeting.status = MeetingStatus.COMPLETED
    from app.services.meeting_sections import MeetingSectionService
    await MeetingSectionService(session).finish(meeting)
    await session.flush()
    llm = getattr(app_state, "llm_provider", None)
    job = None
    recording_job = None
    if not was_completed:
        from app.services.recording import RecordingService
        recording_job = await RecordingService(session).request(meeting)
    if not was_completed and llm and callable(getattr(llm, "polish_transcript", None)):
        try:
            job = await TranscriptPolishService(session, llm).request(meeting_id, actor)
        except ApiError as exc:
            if exc.code != "TRANSCRIPT_INVALID":
                raise
    await session.commit()
    await session.refresh(meeting)
    for queued_job in (job, recording_job):
        if queued_job and queued_job.status == JobStatus.QUEUED:
            try:
                app_state.enqueue_job(queued_job.id, app_state.settings)
            except Exception:
                # Reconciler discovers the durable job; never undo completion.
                pass
    return service._meeting_response(meeting, permission)
