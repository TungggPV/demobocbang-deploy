"""Authentication use cases; routes only translate HTTP to these operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import TokenService, generate_otp, hash_secret, verify_secret
from app.models.enums import EmailOtpPurpose
from app.models.user import EmailOtp, User
from app.providers.email import EmailSender
from app.schemas.auth import AuthSessionResponse, UserResponse


class AuthService:
    def __init__(
        self, session: AsyncSession, settings: Settings, email_sender: EmailSender
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_sender = email_sender
        self._tokens = TokenService(
            settings.jwt_secret,
            settings.access_token_ttl_seconds,
            settings.refresh_token_ttl_seconds,
        )

    async def register(self, email: str, password: str, full_name: str) -> User:
        if await self._user_by_email(email) is not None:
            raise ApiError(
                "EMAIL_ALREADY_REGISTERED", "An account already exists for this email.", 409
            )
        user = User(email=email, password_hash=hash_secret(password), full_name=full_name)
        self._session.add(user)
        await self._session.flush()
        await self._send_new_otp(user, EmailOtpPurpose.VERIFY_EMAIL)
        await self._session.commit()
        return user

    async def login(self, email: str, password: str) -> AuthSessionResponse:
        user = await self._user_by_email(email)
        if (
            user is None
            or user.password_hash is None
            or not verify_secret(password, user.password_hash)
        ):
            raise ApiError("INVALID_CREDENTIALS", "Email or password is incorrect.", 401)
        if not user.is_email_verified:
            raise ApiError("EMAIL_NOT_VERIFIED", "Verify your email before signing in.", 403)
        return self._session_response(user)

    async def verify_email_otp(self, email: str, code: str) -> AuthSessionResponse:
        user = await self._require_user(email)
        await self._consume_otp(user, EmailOtpPurpose.VERIFY_EMAIL, code)
        user.is_email_verified = True
        await self._session.commit()
        return self._session_response(user)

    async def resend_email_otp(self, email: str) -> None:
        user = await self._user_by_email(email)
        if user is None or user.is_email_verified:
            return
        await self._send_new_otp(user, EmailOtpPurpose.VERIFY_EMAIL)
        await self._session.commit()

    async def start_password_reset(self, email: str) -> None:
        user = await self._user_by_email(email)
        if user is None:
            return
        await self._send_new_otp(user, EmailOtpPurpose.RESET_PASSWORD)
        await self._session.commit()

    async def reset_password(self, email: str, code: str, new_password: str) -> None:
        user = await self._require_user(email)
        await self._consume_otp(user, EmailOtpPurpose.RESET_PASSWORD, code)
        user.password_hash = hash_secret(new_password)
        await self._session.commit()

    async def refresh(self, refresh_token: str) -> AuthSessionResponse:
        user_id = self._tokens.verify(refresh_token, "refresh")
        user = await self._session.get(User, user_id)
        if user is None or not user.is_email_verified:
            raise ApiError("AUTH_REQUIRED", "Authentication is required.", 401)
        return self._session_response(user)

    async def user_from_access_token(self, token: str) -> User:
        user_id = self._tokens.verify(token, "access")
        user = await self._session.get(User, user_id)
        if user is None or not user.is_email_verified:
            raise ApiError("AUTH_REQUIRED", "Authentication is required.", 401)
        return user

    async def update_profile(self, user: User, full_name: str) -> User:
        user.full_name = full_name
        await self._session.commit()
        await self._session.refresh(user)
        return user

    async def update_password(
        self, user: User, current_password: str | None, new_password: str
    ) -> User:
        """Set a first password for OAuth users or change an existing password."""
        if user.password_hash is not None and (
            current_password is None
            or not verify_secret(current_password, user.password_hash)
        ):
            raise ApiError(
                "CURRENT_PASSWORD_INVALID",
                "Mật khẩu hiện tại không đúng.",
                400,
            )
        user.password_hash = hash_secret(new_password)
        await self._session.commit()
        await self._session.refresh(user)
        return user

    async def google_user(self, subject: str, email: str, full_name: str, avatar_url: str | None = None) -> User:
        user = await self._session.scalar(select(User).where(User.google_subject == subject))
        if user is None:
            user = await self._user_by_email(email)
            if user is None:
                user = User(
                    email=email,
                    google_subject=subject,
                    full_name=full_name or email,
                    avatar_url=avatar_url,
                    is_email_verified=True,
                )
                self._session.add(user)
            elif user.google_subject is None:
                user.google_subject = subject
                user.is_email_verified = True
                if avatar_url:
                    user.avatar_url = avatar_url
            elif user.google_subject != subject:
                raise ApiError(
                    "GOOGLE_ACCOUNT_CONFLICT",
                    "This email is linked to another Google account.",
                    409,
                )
        user.is_email_verified = True
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        await self._session.commit()
        return user

    def issue_oauth_state(self) -> str:
        return self._tokens.issue_oauth_state()

    def verify_oauth_state(self, state: str) -> None:
        self._tokens.verify_oauth_state(state)

    def session_response(self, user: User) -> AuthSessionResponse:
        return self._session_response(user)

    async def _send_new_otp(self, user: User, purpose: EmailOtpPurpose) -> None:
        now = _now()
        active_otps = await self._session.scalars(
            select(EmailOtp).where(
                EmailOtp.user_id == user.id,
                EmailOtp.purpose == purpose,
                EmailOtp.consumed_at.is_(None),
            )
        )
        for otp in active_otps:
            otp.consumed_at = now
        code = generate_otp()
        self._session.add(
            EmailOtp(
                user_id=user.id,
                purpose=purpose,
                code_hash=hash_secret(code),
                expires_at=now + timedelta(seconds=self._settings.email_otp_ttl_seconds),
                attempt_count=0,
            )
        )
        await self._session.flush()
        await self._email_sender.send_otp(user.email, code, purpose.value)

    async def _consume_otp(self, user: User, purpose: EmailOtpPurpose, code: str) -> None:
        otp = await self._session.scalar(self._active_otp_query(user, purpose))
        if otp is None:
            raise ApiError("OTP_INVALID", "The verification code is invalid or expired.", 400)
        if not verify_secret(code, otp.code_hash):
            otp.attempt_count += 1
            if otp.attempt_count >= self._settings.email_otp_max_attempts:
                otp.consumed_at = _now()
            await self._session.commit()
            raise ApiError("OTP_INVALID", "The verification code is invalid or expired.", 400)
        otp.consumed_at = _now()

    def _active_otp_query(self, user: User, purpose: EmailOtpPurpose) -> Select[tuple[EmailOtp]]:
        return (
            select(EmailOtp)
            .where(
                EmailOtp.user_id == user.id,
                EmailOtp.purpose == purpose,
                EmailOtp.consumed_at.is_(None),
                EmailOtp.expires_at > _now(),
            )
            .order_by(EmailOtp.created_at.desc())
            .with_for_update()
        )

    async def _user_by_email(self, email: str) -> User | None:
        return await self._session.scalar(select(User).where(User.email == email))

    async def _require_user(self, email: str) -> User:
        user = await self._user_by_email(email)
        if user is None:
            raise ApiError("OTP_INVALID", "The verification code is invalid or expired.", 400)
        return user

    def _session_response(self, user: User) -> AuthSessionResponse:
        access_token, refresh_token = self._tokens.issue_pair(user.id)
        return AuthSessionResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user=UserResponse.model_validate(user),
        )


def _now() -> datetime:
    return datetime.now(UTC)
