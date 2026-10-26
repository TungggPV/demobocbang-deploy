"""Authenticated media upload/list/delete and durable job status endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.responses import Response as FastAPIResponse

from app.api.v1.routes.auth import CurrentUserDependency
from app.core.config import Settings
from app.core.dependencies import get_database, get_settings
from app.core.security import TokenService
from app.db.session import Database
from app.providers.storage import MediaStorage
from app.schemas.media import (
    JobResponse,
    MediaFileResponse,
    MediaPlaybackResponse,
    RecordingResponse,
)
from app.services.media import MediaService
from app.services.recording import RecordingService
from app.workers.lifecycle import fail_enqueue

meetings_router = APIRouter(prefix="/meetings")
jobs_router = APIRouter(prefix="/jobs")


async def get_media_service(request: Request) -> AsyncIterator[MediaService]:
    database: Database = get_database(request)
    settings: Settings = get_settings(request)
    storage = cast(MediaStorage, request.app.state.media_storage)
    async with database.session() as session:
        yield MediaService(session, settings, storage)


MediaServiceDependency = Annotated[MediaService, Depends(get_media_service)]


@meetings_router.get("/{meeting_id}/recording", response_model=RecordingResponse)
async def recording_status(meeting_id: UUID, request: Request, user: CurrentUserDependency) -> RecordingResponse:
    async with get_database(request).session() as session:
        return await RecordingService(session).status(meeting_id, user)


@meetings_router.post("/{meeting_id}/recording", response_model=RecordingResponse, status_code=202)
async def prepare_recording(meeting_id: UUID, request: Request, user: CurrentUserDependency) -> RecordingResponse:
    async with get_database(request).session() as session:
        service = RecordingService(session)
        job = await service.prepare(meeting_id, user)
        if job.status == "queued":
            try:
                request.app.state.enqueue_job(job.id, get_settings(request))
            except Exception:
                await fail_enqueue(get_database(request), job.id)
        return await service.status(meeting_id, user)


@meetings_router.post(
    "/{meeting_id}/media", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED
)
async def upload_media(
    meeting_id: UUID,
    file: Annotated[UploadFile, File(...)],
    user: CurrentUserDependency,
    service: MediaServiceDependency,
    request: Request,
) -> JobResponse:
    job = await service.upload(meeting_id, user, file)
    try:
        request.app.state.enqueue_job(job.id, get_settings(request))
    except Exception:
        await fail_enqueue(get_database(request), job.id)
        from app.core.errors import ApiError
        raise ApiError("QUEUE_ENQUEUE_FAILED", "The media job could not be queued.", 503)
    return job


@meetings_router.get("/{meeting_id}/media", response_model=list[MediaFileResponse])
async def list_media(
    meeting_id: UUID, user: CurrentUserDependency, service: MediaServiceDependency
) -> list[MediaFileResponse]:
    return await service.list_media(meeting_id, user)


@meetings_router.post("/{meeting_id}/media/{media_id}/playback-url", response_model=MediaPlaybackResponse)
async def create_playback_url(
    meeting_id: UUID, media_id: UUID, request: Request, user: CurrentUserDependency,
    service: MediaServiceDependency,
) -> MediaPlaybackResponse:
    settings = get_settings(request)
    ttl = settings.storage_presigned_url_ttl_seconds
    url = await service.presigned_url(meeting_id, media_id, user, ttl)
    if url is not None:
        return MediaPlaybackResponse(url=url, expires_in_seconds=ttl)
    ttl = settings.media_playback_ticket_ttl_seconds
    tokens = TokenService(settings.jwt_secret, settings.access_token_ttl_seconds, settings.refresh_token_ttl_seconds)
    ticket = tokens.issue_media_playback_ticket(user.id, meeting_id, media_id, ttl)
    return MediaPlaybackResponse(url=str(request.url_for("playback_media", ticket=ticket)), expires_in_seconds=ttl)


@meetings_router.get("/media/playback/{ticket}", name="playback_media")
async def playback_media(ticket: str, request: Request, service: MediaServiceDependency) -> Response:
    settings = get_settings(request)
    tokens = TokenService(settings.jwt_secret, settings.access_token_ttl_seconds, settings.refresh_token_ttl_seconds)
    _, meeting_id, media_id = tokens.verify_media_playback_ticket(ticket)
    media = await service.media_for_playback_ticket(meeting_id, media_id)
    start, end = _parse_range(request.headers.get("range"), media.size_bytes)
    partial = request.headers.get("range") is not None
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=300"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{media.size_bytes}"
    async def chunks():
        for offset in range(start, end + 1, 1024 * 1024):
            yield await service.read_ticket_media_range(media, offset, min(end, offset + 1024 * 1024 - 1))
    return StreamingResponse(chunks(), status_code=status.HTTP_206_PARTIAL_CONTENT if partial else status.HTTP_200_OK, media_type=media.mime_type, headers=headers)


@meetings_router.get("/{meeting_id}/media/{media_id}/stream")
async def stream_media(
    meeting_id: UUID, media_id: UUID, request: Request, user: CurrentUserDependency,
    service: MediaServiceDependency,
) -> Response:
    """Serve authenticated byte ranges so the browser can seek in uploaded media."""
    # The browser's Range header is intentionally parsed here, after the service
    # has established both meeting membership and media ownership.
    range_header = request.headers.get("range")
    media_list = await service.list_media(meeting_id, user)
    media_info = next((item for item in media_list if item.id == media_id), None)
    if media_info is None:
        from app.core.errors import ApiError
        raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
    start, end = _parse_range(range_header, media_info.size_bytes)
    content, media = await service.read_media_range(meeting_id, media_id, user, start, end)
    partial = range_header is not None
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(len(content))}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{media.size_bytes}"
    return FastAPIResponse(content=content, status_code=status.HTTP_206_PARTIAL_CONTENT if partial else status.HTTP_200_OK, media_type=media.mime_type, headers=headers)


@meetings_router.get("/{meeting_id}/media/{media_id}/download")
async def download_media(
    meeting_id: UUID, media_id: UUID, request: Request, user: CurrentUserDependency, service: MediaServiceDependency
) -> Response:
    settings = get_settings(request)
    url = await service.presigned_url(
        meeting_id, media_id, user, settings.storage_presigned_url_ttl_seconds, download=True
    )
    if url is not None:
        return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    media_list = await service.list_media(meeting_id, user)
    media_info = next((item for item in media_list if item.id == media_id), None)
    if media_info is None:
        from app.core.errors import ApiError
        raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
    media = await service.media_for_playback_ticket(meeting_id, media_id)
    async def chunks():
        for offset in range(0, media_info.size_bytes, 1024 * 1024):
            yield await service.read_ticket_media_range(media, offset, min(media_info.size_bytes - 1, offset + 1024 * 1024 - 1))
    return StreamingResponse(chunks(), media_type=media_info.mime_type, headers={"Content-Disposition": f"attachment; filename=media; filename*=UTF-8''{quote(media_info.original_filename)}", "Accept-Ranges": "bytes", "Content-Length": str(media_info.size_bytes)})


@meetings_router.delete("/{meeting_id}/media/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    meeting_id: UUID,
    media_id: UUID,
    user: CurrentUserDependency,
    service: MediaServiceDependency,
) -> Response:
    await service.delete_media(meeting_id, media_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@jobs_router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID, user: CurrentUserDependency, service: MediaServiceDependency
) -> JobResponse:
    return await service.get_job(job_id, user)


def _parse_range(value: str | None, size: int) -> tuple[int, int]:
    if not value:
        return 0, size - 1
    from app.core.errors import ApiError
    if not value.startswith("bytes=") or "," in value:
        raise ApiError("MEDIA_RANGE_INVALID", "The requested media range is invalid.", 416)
    start_text, _, end_text = value.removeprefix("bytes=").partition("-")
    try:
        if not start_text:
            length = int(end_text)
            if length <= 0:
                raise ValueError
            return max(0, size - length), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise ApiError("MEDIA_RANGE_INVALID", "The requested media range is invalid.", 416) from exc
    if start < 0 or end < start or start >= size:
        raise ApiError("MEDIA_RANGE_INVALID", "The requested media range is invalid.", 416)
    return start, min(end, size - 1)
