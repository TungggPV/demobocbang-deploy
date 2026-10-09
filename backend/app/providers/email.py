"""Email delivery boundary for account verification and password reset."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.errors import ApiError


class EmailSender(Protocol):
    async def send_otp(self, recipient: str, code: str, purpose: str) -> None: ...
    async def send_invitation(self, recipient: str, meeting_title: str, permission: str, url: str) -> None: ...


class UnavailableEmailSender:
    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        raise ApiError("EMAIL_DELIVERY_UNAVAILABLE", "Email delivery is not configured.", 503)

    async def send_invitation(self, recipient: str, meeting_title: str, permission: str, url: str) -> None:
        raise ApiError("EMAIL_DELIVERY_UNAVAILABLE", "Email delivery is not configured.", 503)


class ConsoleEmailSender:
    """Console/logger email sender for local development testing."""

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        print(
            f"\n==========================================\n"
            f"  [DEV EMAIL OTP]\n"
            f"  To: {recipient}\n"
            f"  Purpose: {purpose}\n"
            f"  Verification Code: {code}\n"
            f"==========================================\n",
            flush=True,
        )

    async def send_invitation(self, recipient: str, meeting_title: str, permission: str, url: str) -> None:
        print(f"[DEV EMAIL INVITATION] To: {recipient} | Meeting: {meeting_title} | Permission: {permission} | URL: {url}", flush=True)


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        if not all((settings.email_from, settings.email_smtp_host)):
            raise ValueError("EMAIL_FROM and EMAIL_SMTP_HOST are required for SMTP email delivery.")
        self._sender = settings.email_from
        self._host = settings.email_smtp_host
        self._port = settings.email_smtp_port
        self._username = settings.email_smtp_username
        self._password = settings.email_smtp_password
        self._use_tls = settings.email_smtp_use_tls

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        subject = (
            "Verify your Bóc Băng AI email"
            if purpose == "verify_email"
            else "Reset your Bóc Băng AI password"
        )
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(f"Your verification code is {code}. It expires in 10 minutes.")
        try:
            await asyncio.to_thread(self._send, message)
        except OSError as exc:
            raise ApiError("EMAIL_DELIVERY_FAILED", "Unable to send email.", 502) from exc

    async def send_invitation(self, recipient: str, meeting_title: str, permission: str, url: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = recipient
        message["Subject"] = f"Bạn được mời cộng tác: {meeting_title}"
        message.set_content(
            f"Bạn được mời vào bản ghi “{meeting_title}” với quyền {permission}.\n\n"
            f"Mở bản ghi: {url}\n\nNếu bạn chưa đăng nhập, hãy dùng đúng địa chỉ email nhận thư này."
        )
        try:
            await asyncio.to_thread(self._send, message)
        except OSError as exc:
            raise ApiError("EMAIL_DELIVERY_FAILED", "Unable to send email.", 502) from exc

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=15) as client:
            if self._use_tls:
                client.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(message)


class BrevoEmailSender:
    """Send transactional OTP messages through Brevo's HTTPS API."""

    _API_URL = "https://api.brevo.com/v3/smtp/email"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not all((settings.email_api_key, settings.email_from)):
            raise ValueError("EMAIL_API_KEY and EMAIL_FROM are required for Brevo delivery.")
        self._api_key = settings.email_api_key
        self._sender = settings.email_from
        self._expires_in_minutes = max(1, settings.email_otp_ttl_seconds // 60)
        self._transport = transport

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        is_verification = purpose == "verify_email"
        subject = (
            "Mã xác minh tài khoản Bóc Băng"
            if is_verification
            else "Mã đặt lại mật khẩu Bóc Băng"
        )
        action = "xác minh tài khoản" if is_verification else "đặt lại mật khẩu"
        payload = {
            "sender": {"name": "Bóc Băng", "email": self._sender},
            "to": [{"email": recipient}],
            "subject": subject,
            "textContent": (
                f"Mã {action} của bạn là: {code}\n\n"
                f"Mã hết hạn sau {self._expires_in_minutes} phút.\n"
                "Nếu bạn không yêu cầu mã này, hãy bỏ qua email."
            ),
        }
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.post(
                    self._API_URL,
                    headers={
                        "accept": "application/json",
                        "api-key": self._api_key,
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ApiError("EMAIL_DELIVERY_FAILED", "Unable to send email.", 502) from exc

    async def send_invitation(self, recipient: str, meeting_title: str, permission: str, url: str) -> None:
        labels = {"read": "chỉ xem", "comment": "xem và bình luận", "edit": "chỉnh sửa"}
        payload = {
            "sender": {"name": "Bóc Băng", "email": self._sender},
            "to": [{"email": recipient}],
            "subject": f"Bạn được mời cộng tác: {meeting_title}",
            "textContent": (
                f"Bạn được mời vào bản ghi “{meeting_title}” với quyền {labels.get(permission, permission)}.\n\n"
                f"Mở bản ghi: {url}\n\n"
                "Nếu bạn chưa đăng nhập, hãy dùng đúng địa chỉ email nhận thư này."
            ),
        }
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.post(
                    self._API_URL,
                    headers={"accept": "application/json", "api-key": self._api_key, "content-type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ApiError("EMAIL_DELIVERY_FAILED", "Unable to send email.", 502) from exc


def build_email_sender(settings: Settings) -> EmailSender:
    provider = (settings.email_provider or "").strip().lower()
    if provider == "brevo":
        try:
            return BrevoEmailSender(settings)
        except ValueError:
            return UnavailableEmailSender()
    if provider == "smtp":
        try:
            return SmtpEmailSender(settings)
        except ValueError:
            return UnavailableEmailSender()
    is_production = settings.environment.strip().lower() in {"production", "prod"}
    if not is_production and (
        provider in ("console", "dev", "local")
        or (not provider and settings.environment == "development")
    ):
        return ConsoleEmailSender()
    return UnavailableEmailSender()
