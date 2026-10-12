"""Public authentication and user-profile endpoints."""

from __future__ import annotations

import hmac
from typing import Annotated, cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.dependencies import DatabaseSessionDependency, get_settings
from app.core.errors import ApiError
from app.models.user import User
from app.providers.email import EmailSender
from app.schemas.auth import (
    AuthSessionResponse,
    EmailRequest,
    LoginRequest,
    OtpRequest,
    PasswordUpdateRequest,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    TokenRefreshRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services.auth import AuthService

router = APIRouter(prefix="/auth")
bearer_scheme = HTTPBearer(auto_error=False)


async def get_auth_service(request: Request, session: DatabaseSessionDependency) -> AuthService:
    settings: Settings = get_settings(request)
    email_sender = cast(EmailSender, request.app.state.email_sender)
    return AuthService(session, settings, email_sender)


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(
    service: AuthServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Resolve a verified bearer-token user for protected API slices."""
    if credentials is None:
        raise ApiError("AUTH_REQUIRED", "Authentication is required.", 401)
    return await service.user_from_access_token(credentials.credentials)


CurrentUserDependency = Annotated[User, Depends(get_current_user)]


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, service: AuthServiceDependency) -> RegisterResponse:
    user = await service.register(payload.email, payload.password, payload.full_name)
    return RegisterResponse(user=UserResponse.model_validate(user))


@router.post("/verify-email-otp", response_model=AuthSessionResponse)
async def verify_email_otp(
    payload: OtpRequest, service: AuthServiceDependency
) -> AuthSessionResponse:
    return await service.verify_email_otp(payload.email, payload.code)


@router.post("/resend-email-otp", status_code=status.HTTP_204_NO_CONTENT)
async def resend_email_otp(payload: EmailRequest, service: AuthServiceDependency) -> Response:
    await service.resend_email_otp(payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/login", response_model=AuthSessionResponse)
async def login(payload: LoginRequest, service: AuthServiceDependency) -> AuthSessionResponse:
    return await service.login(payload.email, payload.password)


@router.post("/refresh", response_model=AuthSessionResponse)
async def refresh(
    payload: TokenRefreshRequest, service: AuthServiceDependency
) -> AuthSessionResponse:
    return await service.refresh(payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    """Stateless tokens have no server-side session record to revoke."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(payload: EmailRequest, service: AuthServiceDependency) -> Response:
    await service.start_password_reset(payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordRequest, service: AuthServiceDependency) -> Response:
    await service.reset_password(payload.email, payload.code, payload.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse)
async def current_user(
    user: CurrentUserDependency,
) -> UserResponse:
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
async def update_current_user(
    payload: UserUpdateRequest,
    user: CurrentUserDependency,
    service: AuthServiceDependency,
) -> UserResponse:
    return UserResponse.model_validate(await service.update_profile(user, payload.full_name))


@router.patch("/me/password", response_model=UserResponse)
async def update_current_user_password(
    payload: PasswordUpdateRequest,
    user: CurrentUserDependency,
    service: AuthServiceDependency,
) -> UserResponse:
    return UserResponse.model_validate(
        await service.update_password(user, payload.current_password, payload.new_password)
    )


@router.get("/google/start", include_in_schema=False)
async def google_start(request: Request, service: AuthServiceDependency) -> RedirectResponse:
    settings = get_settings(request)
    _require_google_configuration(settings)
    state = service.issue_oauth_state()
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }
    )
    response = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")
    response.set_cookie(
        "bocbang_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=settings.environment not in {"development", "test"},
        samesite="lax",
    )
    return response


@router.get("/google/callback", include_in_schema=False)
async def google_callback(
    request: Request,
    service: AuthServiceDependency,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = get_settings(request)
    _require_google_configuration(settings)
    if error or code is None or state is None:
        raise ApiError("GOOGLE_OAUTH_FAILED", "Google sign-in was not completed.", 400)
    cookie_state = request.cookies.get("bocbang_oauth_state")
    if cookie_state is None or not hmac.compare_digest(cookie_state, state):
        raise ApiError("GOOGLE_OAUTH_FAILED", "Google sign-in state could not be verified.", 400)
    service.verify_oauth_state(state)
    profile = await _google_profile(settings, code)
    email = profile.get("email")
    subject = profile.get("sub")
    if (
        not isinstance(email, str)
        or not isinstance(subject, str)
        or profile.get("email_verified") is not True
    ):
        raise ApiError(
            "GOOGLE_OAUTH_FAILED", "Google did not return a verified email address.", 400
        )
    picture = profile.get("picture")
    user = await service.google_user(
        subject,
        email.strip().lower(),
        str(profile.get("name") or email),
        str(picture) if picture else None,
    )
    session = service.session_response(user)
    fragment = urlencode(
        {"access_token": session.access_token, "refresh_token": session.refresh_token}
    )
    response = RedirectResponse(f"{settings.frontend_origins[0]}/#{fragment}")
    response.delete_cookie("bocbang_oauth_state")
    return response


def _require_google_configuration(settings: Settings) -> None:
    if not all(
        (settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri)
    ):
        raise ApiError("GOOGLE_OAUTH_UNAVAILABLE", "Google sign-in is not configured.", 503)


async def _google_profile(settings: Settings, code: str) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str):
                raise ValueError("missing access token")
            profile_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
            if not isinstance(profile, dict):
                raise ValueError("invalid userinfo")
            return cast(dict[str, object], profile)
    except (httpx.HTTPError, ValueError) as exc:
        raise ApiError(
            "GOOGLE_OAUTH_FAILED", "Google sign-in could not be completed.", 502
        ) from exc
