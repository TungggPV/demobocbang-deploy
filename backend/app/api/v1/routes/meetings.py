"""Authenticated meeting CRUD, collaborator ACL, and owner-managed share links."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.v1.routes.auth import CurrentUserDependency
from app.api.v1.routes.media import MediaServiceDependency
from app.api.v1.routes.exports import ExportServiceDependency
from app.core.dependencies import DatabaseSessionDependency
from app.schemas.transcript import TranscriptResponse
from app.api.v1.routes.transcripts import TranscriptServiceDependency
from app.api.v1.routes.ai import AIServiceDependency
from app.schemas.meeting import (
    MeetingBundleResponse,
    MeetingCreateRequest,
    MeetingMemberCreateRequest,
    MeetingMemberResponse,
    MeetingMemberUpdateRequest,
    MeetingResponse,
    MeetingUpdateRequest,
    ShareLinkCreateRequest,
    ShareLinkCreatedResponse,
    ShareLinkResponse,
    ShareLinkUpdateRequest,
)
from app.models.enums import MeetingSourceType, MeetingStatus
from app.services.meetings import MeetingService, register_bundle_invalidator
import time

_BUNDLE_CACHE: dict[UUID, tuple[float, MeetingBundleResponse]] = {}
_BUNDLE_CACHE_TTL = 15.0

def invalidate_bundle_cache(meeting_id: UUID | None = None) -> None:
    if meeting_id is None:
        _BUNDLE_CACHE.clear()
    else:
        _BUNDLE_CACHE.pop(meeting_id, None)

register_bundle_invalidator(invalidate_bundle_cache)

router = APIRouter(prefix="/meetings")


async def get_meeting_service(session: DatabaseSessionDependency) -> MeetingService:
    return MeetingService(session)


MeetingServiceDependency = Annotated[MeetingService, Depends(get_meeting_service)]


@router.post("", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
async def create_meeting(
    payload: MeetingCreateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> MeetingResponse:
    return await service.create(user, payload)


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    status: MeetingStatus | None = Query(default=None),
    source_type: MeetingSourceType | None = Query(default=None),
) -> list[MeetingResponse]:
    return await service.list_for_user(user, limit=limit, offset=offset, query_text=q, status=status, source_type=source_type)


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: UUID, user: CurrentUserDependency, service: MeetingServiceDependency
) -> MeetingResponse:
    return await service.get(meeting_id, user)


@router.get("/{meeting_id}/bundle", response_model=MeetingBundleResponse)
async def get_meeting_bundle(
    meeting_id: UUID,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
    transcript_service: TranscriptServiceDependency,
    media_service: MediaServiceDependency,
    ai_service: AIServiceDependency,
) -> MeetingBundleResponse:
    now = time.monotonic()
    cached = _BUNDLE_CACHE.get(meeting_id)
    if cached is not None and (now - cached[0]) < _BUNDLE_CACHE_TTL:
        await service.require_access(meeting_id, user, "read")
        return cached[1]

    meeting, permission = await service.require_access(meeting_id, user, "read")
    meeting_res = service._meeting_response(meeting, permission)

    try:
        transcript_res = await transcript_service.current(meeting_id, user)
    except Exception:
        transcript_res = None

    try:
        media_res = await media_service.list_media(meeting_id, user)
    except Exception:
        media_res = []

    try:
        speakers_res = await transcript_service.list_speakers(meeting_id, user)
    except Exception:
        speakers_res = []

    try:
        translations_res = await ai_service.list_translations(meeting_id, user)
    except Exception:
        translations_res = []

    bundle = MeetingBundleResponse(
        meeting=meeting_res,
        transcript=transcript_res,
        media=media_res,
        speakers=speakers_res,
        translations=translations_res,
    )
    _BUNDLE_CACHE[meeting_id] = (now, bundle)
    return bundle


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    meeting_id: UUID,
    payload: MeetingUpdateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> MeetingResponse:
    return await service.update(meeting_id, user, payload)


@router.post("/{meeting_id}/complete", response_model=MeetingResponse)
async def complete_meeting(
    meeting_id: UUID, user: CurrentUserDependency, service: MeetingServiceDependency
) -> MeetingResponse:
    return await service.complete(meeting_id, user)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: UUID,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
    media_service: MediaServiceDependency,
    export_service: ExportServiceDependency,
) -> Response:
    meeting, _ = await service.require_access(meeting_id, user, "owner")
    await media_service.purge_meeting_storage(meeting_id)
    await export_service.purge_meeting_storage(meeting_id)
    await service.delete(meeting_id, user, meeting=meeting)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{meeting_id}/members", response_model=list[MeetingMemberResponse])
async def list_members(
    meeting_id: UUID, user: CurrentUserDependency, service: MeetingServiceDependency
) -> list[dict[str, object]]:
    return await service.list_members(meeting_id, user)


@router.post(
    "/{meeting_id}/members",
    response_model=MeetingMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    meeting_id: UUID,
    payload: MeetingMemberCreateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> dict[str, object]:
    return await service.add_member(meeting_id, user, payload)


@router.patch("/{meeting_id}/members/{user_id}", response_model=MeetingMemberResponse)
async def update_member(
    meeting_id: UUID,
    user_id: UUID,
    payload: MeetingMemberUpdateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> dict[str, object]:
    return await service.update_member(meeting_id, user, user_id, payload.permission)


@router.delete("/{meeting_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    meeting_id: UUID,
    user_id: UUID,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> Response:
    await service.remove_member(meeting_id, user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{meeting_id}/shares",
    response_model=ShareLinkCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_share(
    meeting_id: UUID,
    payload: ShareLinkCreateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> ShareLinkCreatedResponse:
    return await service.create_share(meeting_id, user, payload)


@router.get("/{meeting_id}/shares", response_model=list[ShareLinkResponse])
async def list_shares(
    meeting_id: UUID, user: CurrentUserDependency, service: MeetingServiceDependency
) -> list[ShareLinkResponse]:
    return await service.list_shares(meeting_id, user)


@router.patch("/shares/{share_id}", response_model=ShareLinkResponse)
async def update_share(
    share_id: UUID,
    payload: ShareLinkUpdateRequest,
    user: CurrentUserDependency,
    service: MeetingServiceDependency,
) -> ShareLinkResponse:
    return await service.update_share(share_id, user, payload)


@router.delete("/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    share_id: UUID, user: CurrentUserDependency, service: MeetingServiceDependency
) -> Response:
    await service.revoke_share(share_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
