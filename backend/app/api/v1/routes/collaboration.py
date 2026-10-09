"""Comment APIs and public, capability-limited share resolution."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.v1.routes.auth import CurrentUserDependency
from app.api.v1.routes.meetings import MeetingServiceDependency
from app.core.dependencies import DatabaseSessionDependency
from app.schemas.collaboration import (
    CommentCreateRequest,
    CommentResponse,
    CommentUpdateRequest,
    ShareAccessResponse,
    ShareTranscriptResponse,
)
from app.schemas.meeting import MeetingResponse
from app.services.collaboration import CollaborationService

meetings_router = APIRouter(prefix="/meetings")
comments_router = APIRouter(prefix="/comments")
shares_router = APIRouter(prefix="/shares")


async def get_collaboration_service(session: DatabaseSessionDependency) -> CollaborationService:
    return CollaborationService(session)


CollaborationServiceDependency = Annotated[CollaborationService, Depends(get_collaboration_service)]


@meetings_router.get("/{meeting_id}/comments", response_model=list[CommentResponse])
async def list_comments(
    meeting_id: UUID,
    user: CurrentUserDependency,
    service: CollaborationServiceDependency,
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> list[CommentResponse]:
    return await service.list_comments(meeting_id, user, limit=limit, offset=offset)


@meetings_router.post(
    "/{meeting_id}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED
)
async def create_comment(
    meeting_id: UUID,
    payload: CommentCreateRequest,
    user: CurrentUserDependency,
    service: CollaborationServiceDependency,
) -> CommentResponse:
    return await service.create_comment(meeting_id, user, payload)


@comments_router.patch("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: UUID,
    payload: CommentUpdateRequest,
    user: CurrentUserDependency,
    service: CollaborationServiceDependency,
) -> CommentResponse:
    return await service.update_comment(comment_id, user, payload)


@comments_router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: UUID, user: CurrentUserDependency, service: CollaborationServiceDependency
) -> Response:
    await service.delete_comment(comment_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@shares_router.get("/{token}", response_model=ShareAccessResponse)
async def resolve_share(token: str, service: MeetingServiceDependency) -> ShareAccessResponse:
    meeting, permission = await service.resolve_share(token)
    return ShareAccessResponse(meeting=meeting, permission=permission)


@shares_router.get("/{token}/transcript", response_model=ShareTranscriptResponse)
async def shared_transcript(token: str, service: CollaborationServiceDependency) -> ShareTranscriptResponse:
    return await service.shared_transcript(token)


@shares_router.post("/{token}/redeem", response_model=MeetingResponse)
async def redeem_share(
    token: str, user: CurrentUserDependency, service: MeetingServiceDependency
) -> MeetingResponse:
    return await service.redeem_share(token, user)
