"""Meeting CRUD and centralized resource-level ACL checks."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.enums import MeetingPermission, MeetingSourceType, MeetingStatus
from app.models.meeting import Meeting, MeetingMember, ShareLink
from app.models.transcript import CurrentSegment, Transcript
from app.models.user import User
from app.providers.email import EmailSender
from app.providers.storage import MediaStorage
from app.schemas.auth import UserResponse
from app.schemas.meeting import (
    AccessLevel,
    MeetingCreateRequest,
    MeetingMemberCreateRequest,
    MeetingResponse,
    MeetingUpdateRequest,
    ShareLinkCreatedResponse,
    ShareLinkCreateRequest,
    ShareLinkResponse,
    ShareLinkUpdateRequest,
)
from app.services.email_shares import build_meeting_email_attachments

_PERMISSION_RANK = {
    MeetingPermission.READ: 1,
    MeetingPermission.COMMENT: 2,
    MeetingPermission.EDIT: 3,
    "owner": 4,
}


class MeetingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, owner: User, payload: MeetingCreateRequest) -> MeetingResponse:
        meeting = Meeting(owner_id=owner.id, **payload.model_dump())
        self._session.add(meeting)
        await self._session.commit()
        await self._session.refresh(meeting)
        return self._meeting_response(meeting, "owner")

    async def list_for_user(
        self, user: User, *, limit: int = 100, offset: int = 0, query_text: str | None = None,
        status: MeetingStatus | None = None, source_type: MeetingSourceType | None = None,
    ) -> list[MeetingResponse]:
        query = (
            select(Meeting, MeetingMember.permission)
            .outerjoin(
                MeetingMember,
                and_(
                    MeetingMember.meeting_id == Meeting.id,
                    MeetingMember.user_id == user.id,
                ),
            )
            .where(or_(Meeting.owner_id == user.id, MeetingMember.user_id.is_not(None)))
            .order_by(Meeting.updated_at.desc(), Meeting.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if query_text:
            query = query.where(Meeting.title.ilike(f"%{query_text.strip()}%"))
        if status is not None:
            query = query.where(Meeting.status == status)
        if source_type is not None:
            query = query.where(Meeting.source_type == source_type)
        rows = (await self._session.execute(query)).all()
        return [
            self._meeting_response(meeting, "owner" if meeting.owner_id == user.id else permission)
            for meeting, permission in rows
            if permission is not None or meeting.owner_id == user.id
        ]

    async def get(self, meeting_id: UUID, user: User) -> MeetingResponse:
        meeting, permission = await self.require_access(meeting_id, user, MeetingPermission.READ)
        return self._meeting_response(meeting, permission)

    async def update(
        self, meeting_id: UUID, user: User, payload: MeetingUpdateRequest
    ) -> MeetingResponse:
        meeting, permission = await self.require_access(meeting_id, user, MeetingPermission.EDIT)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(meeting, field, value)
        await self._session.commit()
        await self._session.refresh(meeting)
        return self._meeting_response(meeting, permission)

    async def complete_realtime(self, meeting_id: UUID, user: User) -> MeetingResponse:
        meeting, permission = await self.require_access(
            meeting_id, user, MeetingPermission.EDIT
        )
        if meeting.source_type != MeetingSourceType.REALTIME:
            raise ApiError(
                "MEETING_SOURCE_TYPE_MISMATCH",
                "Only realtime meetings can be completed here.",
                409,
            )
        if meeting.status == MeetingStatus.COMPLETED:
            return self._meeting_response(meeting, permission)
        segment_count = await self._session.scalar(
            select(func.count(CurrentSegment.id))
            .join(Transcript, CurrentSegment.transcript_id == Transcript.id)
            .where(Transcript.meeting_id == meeting.id)
        )
        if not segment_count:
            raise ApiError(
                "TRANSCRIPT_EMPTY",
                "The recording has no confirmed transcript to complete.",
                409,
            )
        meeting.status = MeetingStatus.COMPLETED
        await self._session.commit()
        await self._session.refresh(meeting)
        return self._meeting_response(meeting, permission)

    async def delete(self, meeting_id: UUID, user: User) -> None:
        meeting, _ = await self.require_access(meeting_id, user, "owner")
        await self._session.delete(meeting)
        await self._session.commit()

    async def require_access(
        self, meeting_id: UUID, user: User, required: MeetingPermission | str
    ) -> tuple[Meeting, AccessLevel]:
        meeting = await self._session.get(Meeting, meeting_id)
        if meeting is None:
            raise ApiError("MEETING_NOT_FOUND", "Meeting was not found.", 404)
        permission: AccessLevel
        if meeting.owner_id == user.id:
            permission = "owner"
        else:
            membership = await self._session.get(MeetingMember, (meeting_id, user.id))
            if membership is None:
                raise ApiError("PERMISSION_DENIED", "You do not have access to this meeting.", 403)
            permission = membership.permission
        if _PERMISSION_RANK[permission] < _PERMISSION_RANK[required]:
            raise ApiError("PERMISSION_DENIED", "You do not have permission for this action.", 403)
        return meeting, permission

    async def list_members(self, meeting_id: UUID, user: User) -> list[dict[str, object]]:
        meeting, _ = await self.require_access(meeting_id, user, "owner")
        owner = await self._session.get(User, meeting.owner_id)
        if owner is None:  # The database FK normally guarantees this invariant.
            raise ApiError("MEETING_NOT_FOUND", "Meeting was not found.", 404)
        rows: list[dict[str, object]] = [
            {
                "user_id": owner.id,
                "permission": "owner",
                "created_at": meeting.created_at,
                "user": UserResponse.model_validate(owner),
            }
        ]
        memberships = await self._session.execute(
            select(MeetingMember, User)
            .join(User, User.id == MeetingMember.user_id)
            .where(MeetingMember.meeting_id == meeting_id)
            .order_by(MeetingMember.created_at.asc())
        )
        rows.extend(
            {
                "user_id": member.user_id,
                "permission": member.permission,
                "created_at": member.created_at,
                "user": UserResponse.model_validate(member_user),
            }
            for member, member_user in memberships.all()
        )
        return rows

    async def add_member(
        self, meeting_id: UUID, actor: User, payload: MeetingMemberCreateRequest,
        email_sender: EmailSender | None = None, frontend_url: str | None = None,
        storage: MediaStorage | None = None, max_attachment_bytes: int = 15_000_000,
    ) -> dict[str, object]:
        meeting, _ = await self.require_access(meeting_id, actor, "owner")
        if payload.attachments and storage is None:
            raise ApiError(
                "EMAIL_ATTACHMENT_UNAVAILABLE",
                "Không thể chuẩn bị tệp đính kèm lúc này.",
                503,
            )
        attachments = (
            await build_meeting_email_attachments(
                self._session,
                storage,
                meeting,
                payload.attachments,
                max_attachment_bytes,
            )
            if payload.attachments and storage is not None
            else []
        )
        target = await self._member_target(payload, required=payload.user_id is not None)
        if target is None:
            if payload.email is None or email_sender is None or frontend_url is None:
                raise ApiError(
                    "INVITATION_UNAVAILABLE",
                    "Không thể gửi lời mời qua email lúc này.",
                    503,
                )
            token = secrets.token_urlsafe(32)
            self._session.add(
                ShareLink(
                    meeting_id=meeting.id,
                    created_by=actor.id,
                    token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    permission=payload.permission,
                )
            )
            try:
                await email_sender.send_invitation(
                    payload.email,
                    meeting.title,
                    payload.permission.value,
                    f"{frontend_url.rstrip('/')}/share/{token}",
                    attachments,
                )
                await self._session.commit()
            except Exception:
                await self._session.rollback()
                raise
            return {
                "email": payload.email,
                "permission": payload.permission,
                "status": "invitation_sent",
            }
        if target.id == meeting.owner_id:
            raise ApiError(
                "OWNER_CANNOT_BE_MEMBER", "The meeting owner is not a collaborator.", 400
            )
        existing = await self._session.get(MeetingMember, (meeting_id, target.id))
        if existing is None:
            member = MeetingMember(
                meeting_id=meeting_id, user_id=target.id, permission=payload.permission
            )
            self._session.add(member)
        else:
            member = existing
            member.permission = payload.permission
        await self._session.flush()
        if email_sender is not None and frontend_url is not None:
            await email_sender.send_invitation(
                target.email,
                meeting.title,
                payload.permission.value,
                f"{frontend_url.rstrip('/')}/meetings/{meeting_id}",
                attachments,
            )
        await self._session.commit()
        await self._session.refresh(member)
        return {
            "user_id": target.id,
            "permission": member.permission,
            "created_at": member.created_at,
            "user": UserResponse.model_validate(target),
        }

    async def update_member(
        self, meeting_id: UUID, actor: User, member_id: UUID, permission: MeetingPermission
    ) -> dict[str, object]:
        await self.require_access(meeting_id, actor, "owner")
        member = await self._session.get(MeetingMember, (meeting_id, member_id))
        if member is None:
            raise ApiError("MEMBER_NOT_FOUND", "Collaborator was not found.", 404)
        member.permission = permission
        await self._session.commit()
        target = await self._session.get(User, member_id)
        if target is None:
            raise ApiError("MEMBER_NOT_FOUND", "Collaborator was not found.", 404)
        return {
            "user_id": member.user_id,
            "permission": member.permission,
            "created_at": member.created_at,
            "user": UserResponse.model_validate(target),
        }

    async def remove_member(self, meeting_id: UUID, actor: User, member_id: UUID) -> None:
        await self.require_access(meeting_id, actor, "owner")
        member = await self._session.get(MeetingMember, (meeting_id, member_id))
        if member is None:
            raise ApiError("MEMBER_NOT_FOUND", "Collaborator was not found.", 404)
        await self._session.delete(member)
        await self._session.commit()

    async def list_shares(self, meeting_id: UUID, user: User) -> list[ShareLinkResponse]:
        await self.require_access(meeting_id, user, "owner")
        shares = await self._session.scalars(
            select(ShareLink)
            .where(ShareLink.meeting_id == meeting_id)
            .order_by(ShareLink.created_at.desc())
        )
        return [self._share_response(share) for share in shares]

    async def create_share(
        self, meeting_id: UUID, user: User, payload: ShareLinkCreateRequest
    ) -> ShareLinkCreatedResponse:
        await self.require_access(meeting_id, user, "owner")
        token = secrets.token_urlsafe(32)
        share = ShareLink(
            meeting_id=meeting_id,
            created_by=user.id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            permission=payload.permission,
            expires_at=payload.expires_at,
        )
        self._session.add(share)
        await self._session.commit()
        await self._session.refresh(share)
        return ShareLinkCreatedResponse(**self._share_response(share).model_dump(), token=token)

    async def update_share(
        self, share_id: UUID, user: User, payload: ShareLinkUpdateRequest
    ) -> ShareLinkResponse:
        share = await self._share_for_owner(share_id, user)
        if share.revoked_at is not None:
            raise ApiError("SHARE_REVOKED", "A revoked share link cannot be changed.", 409)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(share, field, value)
        await self._session.commit()
        await self._session.refresh(share)
        return self._share_response(share)

    async def revoke_share(self, share_id: UUID, user: User) -> None:
        share = await self._share_for_owner(share_id, user)
        if share.revoked_at is None:
            share.revoked_at = _now()
            await self._session.commit()

    async def resolve_share(self, token: str) -> tuple[MeetingResponse, MeetingPermission]:
        meeting, permission = await self.resolve_share_entity(token)
        return self._meeting_response(meeting, permission), permission

    async def resolve_share_entity(self, token: str) -> tuple[Meeting, MeetingPermission]:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        share = await self._session.scalar(select(ShareLink).where(ShareLink.token_hash == token_hash))
        if share is None or share.revoked_at is not None or (share.expires_at is not None and share.expires_at <= _now()):
            raise ApiError("SHARE_INVALID", "This share link is invalid, expired, or revoked.", 404)
        meeting = await self._session.get(Meeting, share.meeting_id)
        if meeting is None:
            raise ApiError("SHARE_INVALID", "This share link is invalid, expired, or revoked.", 404)
        return meeting, share.permission

    async def redeem_share(self, token: str, user: User) -> MeetingResponse:
        meeting, permission = await self.resolve_share_entity(token)
        if meeting.owner_id == user.id:
            return self._meeting_response(meeting, "owner")
        member = await self._session.get(MeetingMember, (meeting.id, user.id))
        if member is None:
            member = MeetingMember(meeting_id=meeting.id, user_id=user.id, permission=permission)
            self._session.add(member)
        elif _PERMISSION_RANK[permission] > _PERMISSION_RANK[member.permission]:
            member.permission = permission
        await self._session.commit()
        await self._session.refresh(meeting)
        return self._meeting_response(meeting, member.permission)

    async def _member_target(
        self, payload: MeetingMemberCreateRequest, *, required: bool = True
    ) -> User | None:
        target = (
            await self._session.get(User, payload.user_id)
            if payload.user_id is not None
            else await self._session.scalar(select(User).where(User.email == payload.email))
        )
        if target is None and required:
            raise ApiError("USER_NOT_FOUND", "No user exists for this invitation.", 404)
        return target

    async def _share_for_owner(self, share_id: UUID, user: User) -> ShareLink:
        share = await self._session.get(ShareLink, share_id)
        if share is None:
            raise ApiError("SHARE_NOT_FOUND", "Share link was not found.", 404)
        await self.require_access(share.meeting_id, user, "owner")
        return share

    @staticmethod
    def _meeting_response(meeting: Meeting, permission: AccessLevel | None) -> MeetingResponse:
        if permission is None:
            raise RuntimeError("Meeting response requires an access level.")
        return MeetingResponse(
            id=meeting.id,
            owner_id=meeting.owner_id,
            title=meeting.title,
            description=meeting.description,
            source_type=meeting.source_type,
            language_mode=meeting.language_mode,
            diarization_enabled=meeting.diarization_enabled,
            status=meeting.status,
            duration_ms=meeting.duration_ms,
            created_at=meeting.created_at,
            updated_at=meeting.updated_at,
            permission=permission,
        )

    @staticmethod
    def _share_response(share: ShareLink) -> ShareLinkResponse:
        return ShareLinkResponse.model_validate(share)


def _now() -> datetime:
    return datetime.now(UTC)
