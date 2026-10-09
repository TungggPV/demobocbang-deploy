import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.invitations: list[tuple[str, str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))

    async def send_invitation(
        self, recipient: str, meeting_title: str, permission: str, url: str
    ) -> None:
        self.invitations.append((recipient, meeting_title, permission, url))


def test_meeting_crud_and_collaborator_acl(tmp_path: Path) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'meetings.sqlite3').as_posix()}"
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
        owner = _verified_session(client, sender, "owner@example.com", "Owner")
        collaborator = _verified_session(client, sender, "member@example.com", "Member")
        outsider = _verified_session(client, sender, "outsider@example.com", "Outsider")
        owner_headers = _headers(owner)
        member_headers = _headers(collaborator)
        outsider_headers = _headers(outsider)

        unauthenticated = client.post("/api/v1/meetings", json={})
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "AUTH_REQUIRED"

        created = client.post(
            "/api/v1/meetings",
            headers=owner_headers,
            json={
                "title": "Sprint planning",
                "description": "Initial agenda",
                "source_type": "upload",
                "language_mode": "vi",
                "diarization_enabled": True,
            },
        )
        assert created.status_code == 201
        assert "X-Process-Time-Ms" in created.headers
        meeting = created.json()
        meeting_id = meeting["id"]
        assert meeting["permission"] == "owner"

        forbidden = client.get(f"/api/v1/meetings/{meeting_id}", headers=outsider_headers)
        assert forbidden.status_code == 403
        assert "X-Process-Time-Ms" in forbidden.headers
        invited = client.post(
            f"/api/v1/meetings/{meeting_id}/members",
            headers=owner_headers,
            json={"email": "member@example.com", "permission": "edit"},
        )
        assert invited.status_code == 201
        assert invited.json()["user_id"] == collaborator["user"]["id"]
        assert invited.json()["permission"] == "edit"
        assert sender.invitations[-1] == (
            "member@example.com",
            "Sprint planning",
            "edit",
            f"http://localhost:5173/meetings/{meeting_id}",
        )

        listed_for_member = client.get("/api/v1/meetings", headers=member_headers)
        assert listed_for_member.status_code == 200
        assert [item["id"] for item in listed_for_member.json()] == [meeting_id]
        assert listed_for_member.json()[0]["permission"] == "edit"
        assert [item["id"] for item in client.get(
            "/api/v1/meetings?limit=1&offset=0", headers=member_headers
        ).json()] == [meeting_id]
        edited = client.patch(
            f"/api/v1/meetings/{meeting_id}",
            headers=member_headers,
            json={"title": "Updated planning"},
        )
        assert edited.status_code == 200
        assert edited.json()["title"] == "Updated planning"
        assert (
            client.delete(f"/api/v1/meetings/{meeting_id}", headers=member_headers).status_code
            == 403
        )
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}/members", headers=member_headers).status_code
            == 403
        )

        downgraded = client.patch(
            f"/api/v1/meetings/{meeting_id}/members/{collaborator['user']['id']}",
            headers=owner_headers,
            json={"permission": "read"},
        )
        assert downgraded.status_code == 200
        assert client.patch(
            f"/api/v1/meetings/{meeting_id}",
            headers=member_headers,
            json={"title": "Not permitted"},
        ).status_code == 403
        deleted = client.delete(f"/api/v1/meetings/{meeting_id}", headers=owner_headers)
        assert deleted.status_code == 204
        assert "X-Process-Time-Ms" in deleted.headers
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}", headers=owner_headers).status_code == 404
        )


def test_owner_managed_share_links_only_return_token_once(tmp_path: Path) -> None:
    async def create_schema(database_url: str) -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'shares.sqlite3').as_posix()}"
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
        owner = _verified_session(client, sender, "owner@example.com", "Owner")
        collaborator = _verified_session(client, sender, "shared@example.com", "Shared user")
        headers = _headers(owner)
        collaborator_headers = _headers(collaborator)
        meeting_id = client.post(
            "/api/v1/meetings",
            headers=headers,
            json={"title": "Shareable", "source_type": "upload", "language_mode": "en"},
        ).json()["id"]
        created = client.post(
            f"/api/v1/meetings/{meeting_id}/shares",
            headers=headers,
            json={"permission": "edit"},
        )
        assert created.status_code == 201
        assert created.json()["token"]
        token = created.json()["token"]
        assert client.get(
            f"/api/v1/meetings/{meeting_id}", headers=collaborator_headers
        ).status_code == 403
        redeemed = client.post(
            f"/api/v1/shares/{token}/redeem", headers=collaborator_headers
        )
        assert redeemed.status_code == 200
        assert redeemed.json()["permission"] == "edit"
        assert client.patch(
            f"/api/v1/meetings/{meeting_id}",
            headers=collaborator_headers,
            json={"title": "Edited through shared link"},
        ).status_code == 200
        share_id = created.json()["id"]
        listed = client.get(f"/api/v1/meetings/{meeting_id}/shares", headers=headers)
        assert listed.status_code == 200
        assert "token" not in listed.json()[0]
        assert (
            client.delete(f"/api/v1/meetings/shares/{share_id}", headers=headers).status_code
            == 204
        )
        assert client.patch(
            f"/api/v1/meetings/shares/{share_id}", headers=headers, json={"permission": "read"}
        ).status_code == 409


def _verified_session(
    client: TestClient, sender: CapturingEmailSender, email: str, full_name: str
) -> dict[str, object]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery", "full_name": full_name},
    )
    assert registered.status_code == 201
    code = sender.messages[-1][1]
    verified = client.post("/api/v1/auth/verify-email-otp", json={"email": email, "code": code})
    assert verified.status_code == 200
    return verified.json()


def _headers(session: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}
