import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.v1.routes import auth as auth_routes
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.base import Base
from app.main import create_app
from app.providers.email import BrevoEmailSender, EmailAttachment, UnavailableEmailSender, build_email_sender


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


def test_email_registration_verification_login_and_refresh(tmp_path: Path) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'auth.sqlite3').as_posix()}"
    asyncio.run(create_schema(database_url))
    sender = CapturingEmailSender()
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
        )
    )
    app.state.email_sender = sender

    with TestClient(app) as client:
        registration = client.post(
            "/api/v1/auth/register",
            json={
                "email": "member@example.com",
                "password": "correct horse battery",
                "full_name": "Member",
            },
        )
        assert registration.status_code == 201
        assert registration.json()["user"]["is_email_verified"] is False
        assert registration.json()["user"]["has_password"] is True
        assert sender.messages[0][0] == "member@example.com"
        assert sender.messages[0][2] == "verify_email"

        resent = client.post(
            "/api/v1/auth/resend-email-otp", json={"email": "member@example.com"}
        )
        assert resent.status_code == 204
        assert sender.messages[-1][2] == "verify_email"

        unverified_login = client.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": "correct horse battery"},
        )
        assert unverified_login.status_code == 403
        assert unverified_login.json()["code"] == "EMAIL_NOT_VERIFIED"

        verified = client.post(
            "/api/v1/auth/verify-email-otp",
            json={"email": "member@example.com", "code": sender.messages[-1][1]},
        )
        assert verified.status_code == 200
        session = verified.json()
        assert session["access_token"]
        assert session["refresh_token"]
        assert session["user"]["is_email_verified"] is True

        profile = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"}
        )
        assert profile.status_code == 200
        assert profile.json()["email"] == "member@example.com"

        updated_profile = client.patch(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={"full_name": "Tên hiển thị mới"},
        )
        assert updated_profile.status_code == 200
        assert updated_profile.json()["full_name"] == "Tên hiển thị mới"

        wrong_password = client.patch(
            "/api/v1/auth/me/password",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={"current_password": "không đúng", "new_password": "mật khẩu mới đủ dài"},
        )
        assert wrong_password.status_code == 400
        assert wrong_password.json()["code"] == "CURRENT_PASSWORD_INVALID"

        changed_password = client.patch(
            "/api/v1/auth/me/password",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={
                "current_password": "correct horse battery",
                "new_password": "changed correct horse battery",
            },
        )
        assert changed_password.status_code == 200
        assert changed_password.json()["has_password"] is True
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": "correct horse battery"},
        ).status_code == 401
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": "changed correct horse battery"},
        ).status_code == 200

        refreshed = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != session["access_token"]

        forgot = client.post(
            "/api/v1/auth/forgot-password", json={"email": "member@example.com"}
        )
        assert forgot.status_code == 204
        assert sender.messages[-1][2] == "reset_password"
        reset = client.post(
            "/api/v1/auth/reset-password",
            json={
                "email": "member@example.com",
                "code": sender.messages[-1][1],
                "new_password": "new correct horse battery",
            },
        )
        assert reset.status_code == 204
        relogin = client.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": "new correct horse battery"},
        )
        assert relogin.status_code == 200


def test_console_email_is_never_used_in_production() -> None:
    sender = build_email_sender(
        Settings(_env_file=None, environment="production", email_provider="console")
    )

    assert isinstance(sender, UnavailableEmailSender)


def test_brevo_email_sender_uses_https_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"messageId": "test-message"})

    sender = BrevoEmailSender(
        Settings(
            _env_file=None,
            email_provider="brevo",
            email_api_key="test-api-key",
            email_from="sender@example.com",
        ),
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(sender.send_otp("member@example.com", "123456", "verify_email"))

    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == "test-api-key"
    payload = json.loads(request.content)
    assert payload["sender"]["email"] == "sender@example.com"
    assert payload["to"] == [{"email": "member@example.com"}]
    assert "123456" in payload["textContent"]

    asyncio.run(
        sender.send_invitation(
            "member@example.com",
            "Họp tuần",
            "read",
            "https://example.com/share/token",
            [EmailAttachment("noi-dung.txt", "Xin chào".encode(), "text/plain")],
        )
    )
    invitation = json.loads(requests[-1].content)
    assert invitation["attachment"][0]["name"] == "noi-dung.txt"
    assert invitation["attachment"][0]["content"] == "WGluIGNow6Bv"


def test_brevo_email_sender_maps_provider_failure_to_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, json={"message": "unauthorized"})

    sender = BrevoEmailSender(
        Settings(
            _env_file=None,
            email_provider="brevo",
            email_api_key="invalid-api-key",
            email_from="sender@example.com",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(sender.send_otp("member@example.com", "123456", "verify_email"))

    assert exc_info.value.code == "EMAIL_DELIVERY_FAILED"


def test_brevo_provider_requires_api_key_and_sender() -> None:
    sender = build_email_sender(
        Settings(_env_file=None, environment="production", email_provider="brevo")
    )

    assert isinstance(sender, UnavailableEmailSender)


def test_google_start_requires_configuration_and_uses_signed_state(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'google.sqlite3').as_posix()}"
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
            google_client_id="client-id",
            google_client_secret="client-secret",
            google_redirect_uri="http://localhost:8000/api/v1/auth/google/callback",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/auth/google/start", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "prompt=" not in response.headers["location"]
    assert "bocbang_oauth_state" in response.headers["set-cookie"]


def test_google_account_sets_password_once_and_keeps_both_login_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    async def google_profile(_settings: Settings, _code: str) -> dict[str, object]:
        return {
            "sub": "google-subject-1",
            "email": "google.member@example.com",
            "email_verified": True,
            "name": "Google Member",
            "picture": "https://example.com/avatar.png",
        }

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'google-password.sqlite3').as_posix()}"
    asyncio.run(create_schema(database_url))
    monkeypatch.setattr(auth_routes, "_google_profile", google_profile)
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
            google_client_id="client-id",
            google_client_secret="client-secret",
            google_redirect_uri="http://localhost:8000/api/v1/auth/google/callback",
        )
    )

    with TestClient(app) as client:
        started = client.get("/api/v1/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        callback = client.get(
            f"/api/v1/auth/google/callback?code=test-code&state={state}",
            follow_redirects=False,
        )
        oauth_tokens = parse_qs(urlparse(callback.headers["location"]).fragment)
        oauth_session = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": oauth_tokens["refresh_token"][0]},
        ).json()
        assert oauth_session["user"]["has_password"] is False

        password_setup = client.patch(
            "/api/v1/auth/me/password",
            headers={"Authorization": f"Bearer {oauth_session['access_token']}"},
            json={"new_password": "google account password"},
        )
        assert password_setup.status_code == 200
        assert password_setup.json()["has_password"] is True

        password_login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "google.member@example.com",
                "password": "google account password",
            },
        )
        assert password_login.status_code == 200
        user_id = password_login.json()["user"]["id"]

        started_again = client.get("/api/v1/auth/google/start", follow_redirects=False)
        next_state = parse_qs(urlparse(started_again.headers["location"]).query)["state"][0]
        callback_again = client.get(
            f"/api/v1/auth/google/callback?code=test-code&state={next_state}",
            follow_redirects=False,
        )
        next_tokens = parse_qs(urlparse(callback_again.headers["location"]).fragment)
        next_session = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": next_tokens["refresh_token"][0]},
        )
        assert next_session.status_code == 200
        assert next_session.json()["user"]["id"] == user_id
        assert next_session.json()["user"]["has_password"] is True
