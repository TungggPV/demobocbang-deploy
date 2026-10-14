"""Comment operations guarded by the shared meeting authorization service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.collaboration import Comment
from app.models.enums import MeetingPermission
from app.models.user import User
from app.schemas.collaboration import CommentCreateRequest, CommentResponse, CommentUpdateRequest, ShareTranscriptResponse
from app.services.transcripts import TranscriptService
from app.services.meetings import MeetingService


class CollaborationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._meetings = MeetingService(session)

    async def list_comments(
        self, meeting_id: UUID, user: User, *, limit: int = 100, offset: int = 0
    ) -> list[CommentResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        stmt = (
            select(Comment, User)
            .outerjoin(User, Comment.author_id == User.id)
            .where(Comment.meeting_id == meeting_id, Comment.deleted_at.is_(None))
            .order_by(Comment.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [self._response(comment, author) for comment, author in rows]

    async def create_comment(
        self, meeting_id: UUID, user: User, payload: CommentCreateRequest
    ) -> CommentResponse:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.COMMENT)
        comment = Comment(
            meeting_id=meeting_id,
            author_id=user.id,
            body=payload.body,
            segment_logical_id=payload.segment_logical_id,
        )
        self._session.add(comment)
        await self._session.commit()
        await self._session.refresh(comment)
        return self._response(comment, user)

    async def update_comment(
        self, comment_id: UUID, user: User, payload: CommentUpdateRequest
    ) -> CommentResponse:
        comment = await self._comment(comment_id)
        await self._meetings.require_access(comment.meeting_id, user, MeetingPermission.COMMENT)
        if comment.author_id != user.id:
            raise ApiError("PERMISSION_DENIED", "Only the comment author may update it.", 403)
        comment.body = payload.body
        await self._session.commit()
        return self._response(comment, user)

    async def delete_comment(self, comment_id: UUID, user: User) -> None:
        comment = await self._comment(comment_id)
        _, access = await self._meetings.require_access(
            comment.meeting_id, user, MeetingPermission.COMMENT
        )
        if comment.author_id != user.id and access != "owner":
            raise ApiError(
                "PERMISSION_DENIED", "Only the author or owner may delete this comment.", 403
            )
        comment.deleted_at = datetime.now(UTC)
        await self._session.commit()

    async def shared_transcript(self, token: str) -> ShareTranscriptResponse:
        meeting, permission = await self._meetings.resolve_share_entity(token)
        transcript = await TranscriptService(self._session).current_for_shared_link(meeting)
        return ShareTranscriptResponse(
            meeting=self._meetings._meeting_response(meeting, permission),
            permission=permission,
            transcript=transcript,
        )

    async def _comment(self, comment_id: UUID) -> Comment:
        comment = await self._session.get(Comment, comment_id)
        if comment is None or comment.deleted_at is not None:
            raise ApiError("COMMENT_NOT_FOUND", "Comment was not found.", 404)
        return comment

    @staticmethod
    def _response(item: Comment, user: User | None = None) -> CommentResponse:
        return CommentResponse(
            id=item.id,
            meeting_id=item.meeting_id,
            segment_logical_id=item.segment_logical_id,
            author_id=item.author_id,
            author_user_id=item.author_id,
            author_name=user.full_name if user else None,
            author_avatar_url=user.avatar_url if user else None,
            body=item.body,
            content=item.body,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
