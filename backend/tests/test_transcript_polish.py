import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_realtime import FakeProvider, FakeStream, _create_realtime_meeting, _verified_session
from test_realtime import _app as realtime_app
from test_transcripts import _app, _headers, _seed, _session

from app.models.media import Job
from app.models.meeting import MeetingMember
from app.models.transcript import CurrentSegment, SegmentVersion
from app.providers.contracts import (
    LLMCapabilities,
    LLMProviderError,
    StreamingTranscriptionEvent,
    TranscriptionSegment,
)
from app.providers.gemini import GeminiSummaryProvider
from app.providers.groq import GroqSummaryProvider
from app.providers.llm import FallbackLLMProvider
from app.services.ai import AIJobDispatcher


class Proofreader:
    def __init__(self, invalid=False, fail=False):
        self.calls = []
        self.invalid, self.fail = invalid, fail

    def capabilities(self):
        return LLMCapabilities(True, 131072)

    async def polish_transcript(self, segments, context):
        self.calls.append((segments, context))
        if self.fail:
            raise LLMProviderError("Provider temporarily unavailable.", retryable=True)
        return () if self.invalid else tuple(text + "." for text in segments)


def run(app, job_id):
    return asyncio.run(
        AIJobDispatcher(
            app.state.database,
            app.state.settings,
            None,
            app.state.llm_provider,
            app.state.media_storage,
        ).run(UUID(job_id))
    )


def setup_client(tmp_path):
    app, sender, url = _app(tmp_path)
    app.state.llm_provider = Proofreader()
    queued = []
    app.state.enqueue_job = lambda job_id, settings: queued.append(str(job_id))
    return app, sender, url, queued


def test_complete_proofread_edit_draft_accept_preserves_original_history(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "polish@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        headers = _headers(owner)
        root = f"/api/v1/meetings/{seed['meeting_id']}"
        endpoint = root + "/transcript/polish"
        # Input must include user edits that exist only as edit events (not STT snapshots).
        client.patch(
            f"/api/v1/segments/{seed['first']}",
            headers=headers,
            json={"text": "Edited before ending"},
        )
        original = client.get(root + "/transcript", headers=headers).json()
        assert client.post(root + "/complete", headers=headers).status_code == 200
        assert len(queued) == 1
        assert client.post(root + "/complete", headers=headers).status_code == 200
        assert len(queued) == 1
        assert not run(app, queued[0])
        proposal = client.get(endpoint, headers=headers).json()
        assert proposal["job"]["status"] == "completed"
        assert proposal["source_version_id"] == original["version_id"]
        assert proposal["segments"][0]["original_text"] == "Edited before ending"
        assert proposal["segments"][0]["generated_text"] == "Edited before ending."
        assert client.get(root + "/transcript", headers=headers).json() == original
        assert client.post(endpoint, headers=headers).json()["job"]["id"] == queued[0]
        body = {
            "revision": 0,
            "segments": [
                {"logical_segment_id": row["logical_segment_id"], "text": row["text"]}
                for row in proposal["segments"]
            ],
        }
        body["segments"][0]["text"] = "Human corrected the AI."
        job_url = endpoint + "/" + queued[0]
        saved = client.patch(job_url, headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["segments"][0]["generated_text"] == "Edited before ending."
        assert client.get(root + "/transcript", headers=headers).json() == original
        assert client.post(job_url + "/accept", headers=headers, json=body).status_code == 409
        body["revision"] = 1
        accepted = client.post(job_url + "/accept", headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
        current = client.get(root + "/transcript", headers=headers).json()
        assert current["version_id"] == accepted.json()["accepted_version_id"]
        assert current["segments"][0]["text"] == "Human corrected the AI."
        for before, after in zip(original["segments"], current["segments"], strict=True):
            for field in (
                "logical_segment_id",
                "start_ms",
                "end_ms",
                "speaker_id",
            ):
                assert before[field] == after[field]
        history = client.get(
            root + f"/transcript/versions/{original['version_id']}", headers=headers
        ).json()
        assert [row["text"] for row in history["segments"]] == [
            row["text"] for row in original["segments"]
        ]
        assert (
            client.post(
                job_url + "/accept", headers=headers, json={**body, "revision": 2}
            ).status_code
            == 409
        )
        assert client.post(root + "/complete", headers=headers).status_code == 200
        assert client.get(endpoint, headers=headers).json()["job"]["id"] == queued[0]

        async def verify_stt_text():
            async with app.state.database.session() as session:
                projection = await session.scalar(
                    select(CurrentSegment).where(
                        CurrentSegment.logical_segment_id == UUID(seed["first"])
                    )
                )
                assert projection.original_stt_text == "First segment"
                snapshots = list(
                    await session.scalars(
                        select(SegmentVersion).where(
                            SegmentVersion.logical_segment_id == UUID(seed["first"])
                        )
                    )
                )
                assert all(row.original_stt_text == "First segment" for row in snapshots)

        asyncio.run(verify_stt_text())


def test_acl_input_alignment_and_stale_source_rejection(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "owner@example.com")
        commenter = _session(client, sender, "commenter@example.com")
        outsider = _session(client, sender, "outside@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]), UUID(commenter["user"]["id"]))
        headers = _headers(owner)
        root = f"/api/v1/meetings/{seed['meeting_id']}"
        endpoint = root + "/transcript/polish"
        assert client.post(endpoint, headers=headers).status_code == 409
        client.post(root + "/complete", headers=headers)
        run(app, queued[0])
        proposal = client.get(endpoint, headers=headers).json()
        job_url = endpoint + "/" + queued[0]
        body = {
            "revision": 0,
            "segments": [
                {"logical_segment_id": row["logical_segment_id"], "text": row["text"]}
                for row in proposal["segments"]
            ],
        }
        assert client.get(endpoint, headers=_headers(commenter)).status_code == 200
        assert client.get(endpoint, headers=_headers(outsider)).status_code == 403
        for url_, method in (
            (endpoint, client.post),
            (job_url, client.patch),
            (job_url + "/accept", client.post),
        ):
            assert method(url_, headers=_headers(commenter), json=body).status_code == 403
        assert (
            client.post(
                job_url + "/accept",
                headers=headers,
                json={**body, "segments": body["segments"][:1]},
            ).status_code
            == 422
        )
        duplicate = {**body, "segments": [body["segments"][0]] * 2}
        assert client.patch(job_url, headers=headers, json=duplicate).status_code == 422
        blank = {**body, "segments": [{**row, "text": " "} for row in body["segments"]]}
        assert client.patch(job_url, headers=headers, json=blank).status_code == 422
        client.patch(
            f"/api/v1/segments/{seed['first']}",
            headers=headers,
            json={"text": "Concurrent manual edit"},
        )
        assert client.post(job_url + "/accept", headers=headers, json=body).status_code == 409
        fresh = client.post(endpoint, headers=headers).json()
        assert fresh["job"]["id"] != queued[0]
        assert fresh["segments"][0]["original_text"] == "Concurrent manual edit"


def test_invalid_output_or_provider_failure_keeps_original_and_allows_retry(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    app.state.llm_provider = Proofreader(invalid=True)
    with TestClient(app) as client:
        owner = _session(client, sender, "failure@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        root, headers = f"/api/v1/meetings/{seed['meeting_id']}", _headers(owner)
        before = client.get(root + "/transcript", headers=headers).json()
        client.post(root + "/complete", headers=headers)
        assert not run(app, queued[0])
        proposal = client.get(root + "/transcript/polish", headers=headers).json()
        assert proposal["job"]["status"] == "failed"
        assert client.get(root + "/transcript", headers=headers).json() == before
        app.state.llm_provider = Proofreader(fail=True)
        retry = client.post(root + "/transcript/polish", headers=headers).json()
        assert run(app, retry["job"]["id"])
        assert (
            client.get(root + "/transcript/polish", headers=headers).json()["job"]["status"]
            == "retrying"
        )
        app.state.llm_provider = Proofreader()
        assert not run(app, retry["job"]["id"])
        assert (
            client.get(root + "/transcript/polish", headers=headers).json()["job"]["status"]
            == "completed"
        )


def test_queue_outage_keeps_completed_meeting_and_durable_pending_proposal(tmp_path):
    app, sender, url, _ = setup_client(tmp_path)

    def broken_queue(*args):
        raise RuntimeError("Redis down")

    app.state.enqueue_job = broken_queue
    with TestClient(app) as client:
        owner = _session(client, sender, "outage@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        root, headers = f"/api/v1/meetings/{seed['meeting_id']}", _headers(owner)
        complete = client.post(root + "/complete", headers=headers)
        assert complete.status_code == 200 and complete.json()["status"] == "completed"
        proposal = client.get(root + "/transcript/polish", headers=headers).json()
        assert proposal["job"]["status"] == "queued"
        run(app, proposal["job"]["id"])
        assert (
            client.get(root + "/transcript/polish", headers=headers).json()["job"]["status"]
            == "completed"
        )


def test_empty_meeting_and_unconfigured_llm_can_still_end(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "empty@example.com")
        headers = _headers(owner)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root = f"/api/v1/meetings/{meeting_id}"
        assert client.post(root + "/complete", headers=headers).status_code == 200
        assert client.get(root + "/transcript/polish", headers=headers).json() is None
        seed = _seed(url, UUID(owner["user"]["id"]))
        app.state.llm_provider = None
        root = f"/api/v1/meetings/{seed['meeting_id']}"
        assert client.post(root + "/complete", headers=headers).status_code == 200
        assert client.post(root + "/transcript/polish", headers=headers).status_code == 503
        assert queued == []


def test_explicit_stop_snapshots_last_final_from_provider_close_not_disconnect(tmp_path):
    app, sender, _ = realtime_app(tmp_path)
    provider = FakeProvider()

    class ClosingStream(FakeStream):
        async def send_audio(self, chunk):
            assert chunk == b"\x10\x00" * 32000
            return await super().send_audio(b"audio-chunk")

        async def close(self):
            return (
                StreamingTranscriptionEvent(
                    "final", "vi", TranscriptionSegment(900, 1700, "Final during close")
                ),
            )

    provider.stream = ClosingStream()
    app.state.stt_provider, app.state.llm_provider = provider, Proofreader()
    queued = []
    app.state.enqueue_job = lambda job_id, settings: queued.append(str(job_id))
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            socket.send_bytes(b"\x10\x00" * 32000)
            assert socket.receive_json()["type"] == "transcript.partial"
            assert socket.receive_json()["type"] == "transcript.final"
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["text"] == "Final during close"
            assert socket.receive_json()["status"] == "completed"
        assert len(queued) == 2  # Independent proofreading and retained-audio assembly.
        proposal = client.get(root + "/transcript/polish", headers=headers).json()
        assert [row["original_text"] for row in proposal["segments"]] == [
            "Bản cuối được lưu",
            "Final during close",
        ]


@pytest.mark.parametrize("provider_class", [GeminiSummaryProvider, GroqSummaryProvider])
def test_provider_polish_schema_alignment_and_conservative_instructions(provider_class):
    provider = provider_class("test-key", "test-model", "https://example.invalid")
    captured = []

    async def generate(prompt, schema):
        captured.append((prompt, schema))
        return {"texts": ["Họp lúc 10 giờ.", "Không thay đổi."]}

    provider._generate = generate
    result = asyncio.run(
        provider.polish_transcript(("họp lúc 10 giờ", "không thay đổi"), "context")
    )
    assert result == ("Họp lúc 10 giờ.", "Không thay đổi.")
    assert "NOT a summary" in captured[0][0] and "untrusted quoted data" in captured[0][0]
    assert captured[0][1]["additionalProperties"] is False

    async def invalid(*args):
        return {"texts": ["Missing second segment"]}

    provider._generate = invalid
    with pytest.raises(LLMProviderError, match="alignment"):
        asyncio.run(provider.polish_transcript(("one", "two"), ""))


def test_polishing_uses_configured_fallback():
    primary, secondary = Proofreader(fail=True), Proofreader()
    provider = FallbackLLMProvider(primary, secondary)
    assert asyncio.run(provider.polish_transcript(("Original",), "context")) == ("Original.",)
    assert len(primary.calls) == len(secondary.calls) == 1


def test_long_proposal_is_batched_without_losing_rows_or_context(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "long@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))
        root, headers = f"/api/v1/meetings/{seed['meeting_id']}", _headers(owner)
        client.post(root + "/complete", headers=headers)

        async def extend_snapshot_fixture():
            from uuid import uuid4

            async with app.state.database.session() as session:
                job = await session.get(Job, UUID(queued[0]))
                originals = [
                    {
                        "logical_segment_id": str(uuid4()),
                        "speaker_id": None,
                        "start_ms": index * 1000,
                        "end_ms": (index + 1) * 1000,
                        "text": f"Segment {index}",
                    }
                    for index in range(85)
                ]
                job.payload = {**job.payload, "originals": originals}
                await session.commit()

        asyncio.run(extend_snapshot_fixture())
        run(app, queued[0])
        proposal = client.get(root + "/transcript/polish", headers=headers).json()
        assert proposal["job"]["status"] == "completed"
        assert len(proposal["segments"]) == 85
        assert [len(call[0]) for call in app.state.llm_provider.calls] == [40, 40, 5]
        assert "Segment 40" in app.state.llm_provider.calls[0][1]
        assert "Segment 39" in app.state.llm_provider.calls[1][1]
        assert proposal["segments"][-1]["text"] == "Segment 84."


def test_edit_permission_revoked_before_worker_prevents_provider_call(tmp_path):
    app, sender, url, queued = setup_client(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "acl-owner@example.com")
        editor = _session(client, sender, "acl-editor@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]), UUID(editor["user"]["id"]))
        root, headers = f"/api/v1/meetings/{seed['meeting_id']}", _headers(owner)
        app.state.llm_provider = None
        client.post(root + "/complete", headers=headers)
        app.state.llm_provider = Proofreader()
        editor_id = UUID(editor["user"]["id"])

        async def change_access(permission):
            async with app.state.database.session() as session:
                member = await session.get(MeetingMember, (UUID(seed["meeting_id"]), editor_id))
                member.permission = permission
                await session.commit()

        asyncio.run(change_access("edit"))
        proposal = client.post(root + "/transcript/polish", headers=_headers(editor)).json()
        asyncio.run(change_access("read"))
        assert not run(app, proposal["job"]["id"])
        result = client.get(root + "/transcript/polish", headers=headers).json()
        assert result["job"]["status"] == "failed"
        assert result["job"]["error"]["code"] == "PERMISSION_DENIED"
        assert app.state.llm_provider.calls == []


def test_navigation_disconnect_does_not_queue_proofreading(tmp_path):
    app, sender, _ = realtime_app(tmp_path)
    app.state.stt_provider, app.state.llm_provider = FakeProvider(), Proofreader()
    queued = []
    app.state.enqueue_job = lambda job_id, settings: queued.append(str(job_id))
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            socket.send_bytes(b"audio-chunk")
            socket.receive_json()
            socket.receive_json()
        headers = _headers(owner)
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}", headers=headers).json()["status"]
            != "completed"
        )
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}/transcript/polish", headers=headers).json()
            is None
        )
        assert queued == []
