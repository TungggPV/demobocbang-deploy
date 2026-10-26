import asyncio
import time
from pathlib import Path
from threading import Thread
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.enums import MeetingPermission, MeetingSourceType, TranscriptVersionSource
from app.models.meeting import Meeting, MeetingMember
from app.models.transcript import SegmentVersion, Transcript, TranscriptVersion
from app.providers.contracts import (
    LLMCapabilities,
    LLMProviderError,
    TranscriptionSegment,
    TranslationCapabilities,
    TranslationProviderError,
)
from app.services.ai import AIJobDispatcher


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


class FakeTranslation:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(frozenset({("vi", "en"), ("en", "vi")}))

    async def translate_segments(self, segments: tuple[TranscriptionSegment, ...], source: str, target: str) -> tuple[TranscriptionSegment, ...]:
        if self.fail:
            raise TranslationProviderError("Fake translator unavailable.", retryable=False)
        return tuple(TranscriptionSegment(item.start_ms, item.end_ms, f"{target}:{item.text}") for item in segments)


class FakeLLM:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(structured_output=True, max_context_tokens=4096)

    async def generate_summary(
        self,
        transcript: str,
        prompt: str | None,
        output_language: str,
        template: str | None = None,
    ) -> dict[str, object]:
        if self.fail:
            raise LLMProviderError("Fake LLM unavailable.", retryable=True)
        if template:
            return {
                "overview": f"{output_language}:{template}: {transcript}",
                "key_points": [template],
                "action_items": [],
                "decisions": [],
                "risks_notes": [],
            }
        return {
            "content": f"{output_language}: {prompt}: {transcript}",
        }


def test_vi_en_and_en_vi_translation_preserve_source_and_segment_mapping(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    app.state.translation_provider, app.state.llm_provider = FakeTranslation(), FakeLLM()
    with TestClient(app) as client:
        owner = _session(client, sender, "translation@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        headers = _headers(owner)
        for source, target in (("vi", "en"), ("en", "vi")):
            job = client.post(f"/api/v1/meetings/{seed['meeting_id']}/translations", headers=headers, json={"source_language": source, "target_language": target})
            assert job.status_code == 202
            assert _job(client, headers, job.json()["id"])["status"] == "completed"
        results = client.get(f"/api/v1/meetings/{seed['meeting_id']}/translations", headers=headers).json()
        assert len(results) == 2 and results[0]["segments"][0]["logical_segment_id"] == seed["logical_id"]
        assert _texts(url) == ["Xin chào", "Cuộc họp kết thúc"]


def test_translation_capability_empty_input_and_permissions(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    app.state.translation_provider, app.state.llm_provider = FakeTranslation(), FakeLLM()
    with TestClient(app) as client:
        owner, commenter = _session(client, sender, "translation-owner@example.com"), _session(client, sender, "translation-comment@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]), UUID(commenter["user"]["id"]))
        unsupported = client.post(f"/api/v1/meetings/{seed['meeting_id']}/translations", headers=_headers(owner), json={"source_language": "vi", "target_language": "fr"})
        assert unsupported.status_code == 422
        denied = client.post(f"/api/v1/meetings/{seed['meeting_id']}/translations", headers=_headers(commenter), json={"source_language": "vi", "target_language": "en"})
        assert denied.status_code == 403
        empty = _empty_seed(url, UUID(owner["user"]["id"]))
        assert client.post(f"/api/v1/meetings/{empty}/translations", headers=_headers(owner), json={"source_language": "vi", "target_language": "en"}).status_code == 422
        capabilities = client.get("/api/v1/capabilities").json()
        assert ["en", "vi"] in capabilities["translation"]["language_pairs"]


def test_summary_modes_custom_structure_manual_edits_and_versions(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    app.state.translation_provider, app.state.llm_provider = FakeTranslation(), FakeLLM()
    with TestClient(app) as client:
        owner = _session(client, sender, "summary@example.com")
        commenter = _session(client, sender, "summary-commenter@example.com")
        seed, headers = _seed(url, UUID(owner["user"]["id"]), UUID(commenter["user"]["id"])), _headers(owner)
        endpoint = f"/api/v1/meetings/{seed['meeting_id']}/summaries"

        assert client.post(endpoint, headers=headers, json={"output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "template", "output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "template", "template": "general", "prompt": "Không hợp lệ", "output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "custom", "prompt": "   ", "output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "custom", "template": "general", "prompt": "Không hợp lệ", "output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "custom", "prompt": "x" * 4001, "output_language": "vi"}).status_code == 422
        assert client.post(endpoint, headers=headers, json={"mode": "custom", "prompt": "Tóm tắt", "output_language": "fr"}).status_code == 422

        built_in = client.post(
            endpoint,
            headers=headers,
            json={"mode": "template", "template": "general", "output_language": "vi"},
        )
        assert _job(client, headers, built_in.json()["id"])["status"] == "completed"

        vietnamese_prompt = "Tóm tắt các quyết định và người chịu trách nhiệm."
        vietnamese = client.post(
            endpoint,
            headers=headers,
            json={"mode": "custom", "prompt": vietnamese_prompt, "output_language": "vi"},
        )
        assert _job(client, headers, vietnamese.json()["id"])["status"] == "completed"
        english = client.post(
            endpoint,
            headers=headers,
            json={"mode": "custom", "prompt": "Summarize the main outcomes.", "output_language": "en"},
        )
        assert _job(client, headers, english.json()["id"])["status"] == "completed"

        summaries = client.get(endpoint, headers=headers).json()
        template_vi = next(item for item in summaries if item["mode"] == "template")
        custom_vi = next(item for item in summaries if item["mode"] == "custom" and item["output_language"] == "vi")
        custom_en = next(item for item in summaries if item["mode"] == "custom" and item["output_language"] == "en")
        assert template_vi["template"] == "general"
        assert set(template_vi["current_version"]["content"]) == {"overview", "key_points", "action_items", "decisions", "risks_notes"}
        assert custom_vi["id"] != template_vi["id"] != custom_en["id"]
        assert custom_vi["template"] is None
        assert custom_vi["current_version"]["prompt"] == vietnamese_prompt
        assert custom_vi["current_version"]["content"]["content"].startswith(f"vi: {vietnamese_prompt}")

        regenerated_prompt = "Chỉ tập trung vào các công việc tiếp theo."
        regenerate = client.post(
            endpoint,
            headers=headers,
            json={"mode": "custom", "prompt": regenerated_prompt, "output_language": "vi"},
        )
        assert _job(client, headers, regenerate.json()["id"])["status"] == "completed"
        vi_versions = client.get(f"/api/v1/summaries/{custom_vi['id']}/versions", headers=headers).json()
        en_versions = client.get(f"/api/v1/summaries/{custom_en['id']}/versions", headers=headers).json()
        assert [item["version_no"] for item in vi_versions] == [2, 1]
        assert [item["prompt"] for item in vi_versions] == [regenerated_prompt, vietnamese_prompt]
        assert [item["version_no"] for item in en_versions] == [1]
        assert client.get(f"/api/v1/summaries/{custom_vi['id']}/versions/{vi_versions[1]['id']}", headers=headers).status_code == 200

        edited_content = {
            "content": "Biên bản đã chỉnh sửa\n\nKết luận\nNội dung do người dùng sửa.",
        }
        denied = client.patch(
            f"/api/v1/summaries/{custom_vi['id']}",
            headers=_headers(commenter),
            json={"content": edited_content},
        )
        assert denied.status_code == 403
        edited = client.patch(
            f"/api/v1/summaries/{custom_vi['id']}",
            headers=headers,
            json={"content": edited_content},
        )
        assert edited.status_code == 200
        assert edited.json()["current_version"]["version_no"] == 3
        assert edited.json()["current_version"]["content"] == edited_content
        assert edited.json()["current_version"]["provider"] == "user"
        vi_versions = client.get(f"/api/v1/summaries/{custom_vi['id']}/versions", headers=headers).json()
        assert [item["version_no"] for item in vi_versions] == [3, 2, 1]
        assert vi_versions[0]["prompt"] == regenerated_prompt
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["custom_summary_prompt"] is True
        assert capabilities["summary_templates"] == ["general", "briefing", "voting"]
        assert capabilities["summary_languages"] == ["vi", "en"]

def test_provider_failure_marks_job_failed_with_retryability(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    app.state.translation_provider, app.state.llm_provider = FakeTranslation(fail=True), FakeLLM(fail=True)
    with TestClient(app) as client:
        owner = _session(client, sender, "failure@example.com")
        seed, headers = _seed(url, UUID(owner["user"]["id"])), _headers(owner)
        translation = client.post(f"/api/v1/meetings/{seed['meeting_id']}/translations", headers=headers, json={"source_language": "vi", "target_language": "en"})
        failed_translation = _job(client, headers, translation.json()["id"])
        assert failed_translation["status"] == "failed" and failed_translation["error"]["retryable"] is False
        summary = client.post(f"/api/v1/meetings/{seed['meeting_id']}/summaries", headers=headers, json={"mode": "custom", "prompt": "Tóm tắt cuộc họp.", "output_language": "vi"})
        failed_summary = _job(client, headers, summary.json()["id"])
        assert failed_summary["status"] == "failed" and failed_summary["error"]["retryable"] is True


def test_summary_requires_available_structured_provider(tmp_path: Path) -> None:
    app, sender, url = _app(tmp_path)
    app.state.translation_provider, app.state.llm_provider = FakeTranslation(), None
    with TestClient(app) as client:
        owner = _session(client, sender, "unavailable-summary@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        response = client.post(
            f"/api/v1/meetings/{seed['meeting_id']}/summaries",
            headers=_headers(owner),
            json={"mode": "custom", "prompt": "Tóm tắt cuộc họp.", "output_language": "vi"},
        )
        assert response.status_code == 503


def _app(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ai.sqlite3').as_posix()}"
    async def create_schema() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()
    asyncio.run(create_schema())
    app = create_app(Settings(_env_file=None, database_url=url, jwt_secret="test-secret", frontend_origins="http://localhost:5173", translation_provider="fake", llm_provider="fake"))
    sender = CapturingEmailSender()
    app.state.email_sender = sender

    def enqueue(job_id, _settings) -> None:
        def execute() -> None:
            async def run_until_terminal() -> None:
                dispatcher = AIJobDispatcher(
                    app.state.database,
                    app.state.settings,
                    app.state.translation_provider,
                    app.state.llm_provider,
                    app.state.media_storage,
                )
                while await dispatcher.run(UUID(str(job_id))):
                    pass

            asyncio.run(run_until_terminal())

        Thread(target=execute, daemon=True).start()

    app.state.enqueue_job = enqueue
    return app, sender, url


def _session(client: TestClient, sender: CapturingEmailSender, email: str) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct horse battery", "full_name": email})
    return client.post("/api/v1/auth/verify-email-otp", json={"email": email, "code": sender.messages[-1][1]}).json()


def _headers(session: dict) -> dict[str, str]: return {"Authorization": f"Bearer {session['access_token']}"}


def _job(client: TestClient, headers: dict[str, str], job_id: str) -> dict:
    for _ in range(50):
        value = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        if value["status"] in {"completed", "failed"}: return value
        time.sleep(0.02)
    raise AssertionError("AI job did not finish")


def _seed(url: str, owner_id: UUID, commenter_id: UUID | None = None) -> dict[str, str]:
    async def seed():
        engine = create_async_engine(url); factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            meeting = Meeting(owner_id=owner_id, title="AI", source_type=MeetingSourceType.REALTIME, language_mode="vi")
            transcript = Transcript(meeting=meeting); version = TranscriptVersion(transcript=transcript, version_no=1, source=TranscriptVersionSource.STT, created_by=owner_id)
            first = SegmentVersion(transcript_version=version, sequence=1, start_ms=0, end_ms=1000, text="Xin chào")
            second = SegmentVersion(transcript_version=version, sequence=2, start_ms=1000, end_ms=2000, text="Cuộc họp kết thúc")
            if commenter_id: session.add(MeetingMember(meeting=meeting, user_id=commenter_id, permission=MeetingPermission.COMMENT))
            session.add_all((meeting, transcript, version, first, second)); await session.flush(); meeting.current_transcript_version_id = version.id; await session.commit()
            result = {"meeting_id": str(meeting.id), "logical_id": str(first.logical_segment_id)}
        await engine.dispose(); return result
    return asyncio.run(seed())


def _empty_seed(url: str, owner_id: UUID) -> str:
    async def seed() -> str:
        engine = create_async_engine(url); factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            meeting = Meeting(owner_id=owner_id, title="Empty", source_type=MeetingSourceType.REALTIME, language_mode="vi")
            transcript = Transcript(meeting=meeting); version = TranscriptVersion(transcript=transcript, version_no=1, source=TranscriptVersionSource.STT, created_by=owner_id)
            session.add_all((meeting, transcript, version)); await session.flush(); meeting.current_transcript_version_id = version.id; await session.commit(); value = str(meeting.id)
        await engine.dispose(); return value
    return asyncio.run(seed())


def _texts(url: str) -> list[str]:
    async def read() -> list[str]:
        engine = create_async_engine(url); factory = async_sessionmaker(engine)
        async with factory() as session: values = [item.text for item in await session.scalars(select(SegmentVersion).order_by(SegmentVersion.sequence))]
        await engine.dispose(); return values
    return asyncio.run(read())
