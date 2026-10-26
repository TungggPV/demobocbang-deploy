"""ACL-protected asynchronous translation and summary APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.v1.routes.auth import CurrentUserDependency
from app.core.dependencies import get_database
from app.db.session import Database
from app.providers.contracts import LLMProvider, TranslationProvider
from app.schemas.ai import (
    SummaryCreateRequest,
    SummaryEditRequest,
    SummaryResponse,
    SummaryVersionResponse,
    TranslationCreateRequest,
    TranslationResponse,
)
from app.schemas.media import JobResponse
from app.schemas.transcript_polish import PolishResponse, PolishReviewRequest
from app.services.ai import AIService
from app.services.transcript_polish import TranscriptPolishService
from app.workers.lifecycle import fail_enqueue

meetings_router = APIRouter(prefix="/meetings")
translations_router = APIRouter(prefix="/translations")
summaries_router = APIRouter(prefix="/summaries")


async def get_ai_service(request: Request) -> AsyncIterator[AIService]:
    database: Database = get_database(request)
    async with database.session() as session:
        yield AIService(
            session,
            cast(TranslationProvider | None, request.app.state.translation_provider),
            cast(LLMProvider | None, request.app.state.llm_provider),
        )


AIServiceDependency = Annotated[AIService, Depends(get_ai_service)]


@meetings_router.get("/{meeting_id}/transcript/polish", response_model=PolishResponse | None)
async def latest_polish(meeting_id: UUID, user: CurrentUserDependency, request: Request):
    async with get_database(request).session() as session:
        return await TranscriptPolishService(session, request.app.state.llm_provider).latest(meeting_id, user)


@meetings_router.post("/{meeting_id}/transcript/polish", response_model=PolishResponse, status_code=202)
async def create_polish(meeting_id: UUID, user: CurrentUserDependency, request: Request):
    from app.services.transcript_polish import response
    async with get_database(request).session() as session:
        job = await TranscriptPolishService(session, request.app.state.llm_provider).request(meeting_id, user)
        # Commit exists before publishing; reconciler retries delivery if Redis is down.
        try:
            request.app.state.enqueue_job(job.id, request.app.state.settings)
        except Exception:
            await fail_enqueue(get_database(request), job.id)
        return response(job)


@meetings_router.patch("/{meeting_id}/transcript/polish/{job_id}", response_model=PolishResponse)
async def save_polish(meeting_id: UUID, job_id: UUID, payload: PolishReviewRequest, user: CurrentUserDependency, request: Request):
    async with get_database(request).session() as session:
        return await TranscriptPolishService(session, request.app.state.llm_provider).review(meeting_id, job_id, user, payload, accept=False)


@meetings_router.post("/{meeting_id}/transcript/polish/{job_id}/accept", response_model=PolishResponse)
async def accept_polish(meeting_id: UUID, job_id: UUID, payload: PolishReviewRequest, user: CurrentUserDependency, request: Request):
    async with get_database(request).session() as session:
        return await TranscriptPolishService(session, request.app.state.llm_provider).review(meeting_id, job_id, user, payload, accept=True)


@meetings_router.post("/{meeting_id}/translations", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_translation(meeting_id: UUID, payload: TranslationCreateRequest, user: CurrentUserDependency, service: AIServiceDependency, request: Request) -> JobResponse:
    job = await service.request_translation(meeting_id, user, payload)
    try:
        request.app.state.enqueue_job(job.id, request.app.state.settings)
    except Exception:
        await fail_enqueue(get_database(request), job.id)
        from app.core.errors import ApiError
        raise ApiError("QUEUE_ENQUEUE_FAILED", "The translation job could not be queued.", 503)
    return job


@meetings_router.get("/{meeting_id}/translations", response_model=list[TranslationResponse])
async def list_translations(meeting_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> list[TranslationResponse]:
    return await service.list_translations(meeting_id, user)


@translations_router.get("/{translation_id}", response_model=TranslationResponse)
async def get_translation(translation_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> TranslationResponse:
    return await service.translation(translation_id, user)


@meetings_router.post("/{meeting_id}/summaries", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_summary(meeting_id: UUID, payload: SummaryCreateRequest, user: CurrentUserDependency, service: AIServiceDependency, request: Request) -> JobResponse:
    job = await service.request_summary(meeting_id, user, payload)
    try:
        request.app.state.enqueue_job(job.id, request.app.state.settings)
    except Exception:
        await fail_enqueue(get_database(request), job.id)
        from app.core.errors import ApiError
        raise ApiError("QUEUE_ENQUEUE_FAILED", "The summary job could not be queued.", 503)
    return job


@meetings_router.get("/{meeting_id}/summaries", response_model=list[SummaryResponse])
async def list_summaries(meeting_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> list[SummaryResponse]:
    return await service.list_summaries(meeting_id, user)


@summaries_router.get("/{summary_id}", response_model=SummaryResponse)
async def get_summary(summary_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> SummaryResponse:
    return await service.summary(summary_id, user)


@summaries_router.patch("/{summary_id}", response_model=SummaryResponse)
async def edit_summary(
    summary_id: UUID,
    payload: SummaryEditRequest,
    user: CurrentUserDependency,
    service: AIServiceDependency,
) -> SummaryResponse:
    return await service.edit_summary(summary_id, user, payload)


@summaries_router.get("/{summary_id}/versions", response_model=list[SummaryVersionResponse])
async def get_summary_versions(summary_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> list[SummaryVersionResponse]:
    return await service.summary_versions(summary_id, user)


@summaries_router.get("/{summary_id}/versions/{version_id}", response_model=SummaryVersionResponse)
async def get_summary_version(summary_id: UUID, version_id: UUID, user: CurrentUserDependency, service: AIServiceDependency) -> SummaryVersionResponse:
    return await service.summary_version(summary_id, version_id, user)
