import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_realtime import FakeProvider, _app, _create_realtime_meeting, _verified_session
from test_transcript_polish import run
from test_transcripts import _headers, _seed, _session

from app.providers.contracts import (
    LLMCapabilities,
    StreamingTranscriptionEvent,
    TranscriptionSegment,
)


class ClosingStream:
    def __init__(self, number):
        self.number, self.pending, self.closed = number, False, False

    async def send_audio(self, chunk):
        assert len(chunk) == 32000
        self.pending = True
        return (
            StreamingTranscriptionEvent(
                "partial", "vi", TranscriptionSegment(0, 900, f"Nội dung {self.number}")
            ),
        )

    async def pause(self):
        return ()

    async def resume(self):
        return ()

    async def close(self):
        self.closed = True
        if not self.pending:
            return ()
        self.pending = False
        return (
            StreamingTranscriptionEvent(
                "final", "vi", TranscriptionSegment(0, 900, f"Nội dung {self.number}")
            ),
        )


class SectionProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.sessions = []

    async def open_stream(self, options):
        stream = ClosingStream(len(self.sessions) + 1)
        self.sessions.append(stream)
        return stream


class CapturingLLM:
    def __init__(self, fail_on=None):
        self.inputs, self.fail_on = [], fail_on

    def capabilities(self):
        return LLMCapabilities(True, 10000)

    async def generate_summary(self, transcript, prompt, language, template=None):
        self.inputs.append(transcript)
        if transcript == self.fail_on:
            from app.providers.contracts import LLMProviderError

            raise LLMProviderError("Provider failed.", retryable=False)
        return (
            {
                "overview": transcript,
                "key_points": [],
                "action_items": [],
                "decisions": [],
                "risks_notes": [],
            }
            if template
            else {"content": transcript}
        )


def setup(tmp_path):
    app, sender, url = _app(tmp_path)
    app.state.stt_provider, app.state.llm_provider = SectionProvider(), CapturingLLM()
    jobs = []
    app.state.enqueue_job = lambda job_id, settings: jobs.append(str(job_id))
    return app, sender, url, jobs


def two_sections(client, app, sender):
    owner = _verified_session(client, sender)
    meeting_id = _create_realtime_meeting(client, owner["access_token"])
    root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
    socket_url = f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
    with client.websocket_connect(socket_url) as socket:
        assert socket.receive_json()["type"] == "session.ready"
        socket.send_bytes(b"\x00\x00" * 16000)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_json({"type": "recording.pause", "new_section": True})
        assert socket.receive_json()["text"] == "Nội dung 1"
        paused = socket.receive_json()
        assert paused["type"] == "meeting.sections" and paused["paused"]
        sections = paused["sections"]
        assert [(row["start_ms"], row["end_ms"], row["is_started"]) for row in sections] == [
            (0, 1000, True),
            (1000, None, False),
        ]
        assert len(app.state.stt_provider.sessions) == 1
        socket.send_json({"type": "recording.pause", "new_section": True})
        assert len(socket.receive_json()["sections"]) == 2
        for row, name in zip(sections, ["Kế hoạch", "Ngân sách"], strict=True):
            assert (
                client.patch(
                    root + f"/sections/{row['id']}", headers=headers, json={"title": name}
                ).status_code
                == 200
            )
        socket.send_json({"type": "recording.resume"})
        resumed = socket.receive_json()
        assert resumed["type"] == "recording.resumed" and resumed["fallback_active"] is False
        assert len(app.state.stt_provider.sessions) == 2
        socket.send_bytes(b"\x00\x00" * 16000)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_json({"type": "recording.stop"})
        assert socket.receive_json()["text"] == "Nội dung 2"
        assert socket.receive_json()["status"] == "completed"
    return root, headers, owner


def test_pause_flushes_old_session_resume_restarts_clock_and_names_persist(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        sections = client.get(root + "/sections", headers=headers).json()
        assert [row["title"] for row in sections] == ["Kế hoạch", "Ngân sách"]
        assert [row["end_ms"] for row in sections] == [1000, 2000]
        rows = client.get(root + "/transcript", headers=headers).json()["segments"]
        assert [(row["start_ms"], row["end_ms"], row["text"]) for row in rows] == [
            (0, 900, "Nội dung 1"),
            (1000, 1900, "Nội dung 2"),
        ]


@pytest.mark.parametrize("mode", ["template", "custom"])
def test_each_summary_uses_only_its_session_and_edits_stay_versioned(tmp_path, mode):
    app, sender, _, jobs = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        body = {"scope": "sessions", "mode": mode, "output_language": "vi"}
        body.update({"template": "general"} if mode == "template" else {"prompt": "Các quyết định"})
        response = client.post(root + "/summaries", headers=headers, json=body)
        assert response.status_code == 202
        assert not run(app, response.json()["id"])
        summaries = client.get(root + "/summaries", headers=headers).json()
        assert len(summaries) == 1 and summaries[0]["scope"] == "sessions"
        summary = summaries[0]
        sections = summary["current_version"]["content"]["sessions"]
        assert [row["title"] for row in sections] == ["Kế hoạch", "Ngân sách"]
        key = "overview" if mode == "template" else "content"
        assert [row["content"][key] for row in sections] == ["Nội dung 1", "Nội dung 2"]
        assert app.state.llm_provider.inputs == ["Nội dung 1", "Nội dung 2"]
        sections[0]["content"][key] = "Thư ký sửa phiên thứ nhất"
        edited = client.patch(
            f"/api/v1/summaries/{summary['id']}",
            headers=headers,
            json={"content": {"sessions": sections}, "expected_version_no": 1},
        )
        assert edited.status_code == 200 and edited.json()["current_version"]["version_no"] == 2
        assert (
            edited.json()["current_version"]["content"]["sessions"][1]["content"][key]
            == "Nội dung 2"
        )
        history = client.get(f"/api/v1/summaries/{summary['id']}/versions", headers=headers).json()
        assert history[-1]["content"]["sessions"][0]["content"][key] == "Nội dung 1"
        assert (
            client.patch(
                f"/api/v1/summaries/{summary['id']}",
                headers=headers,
                json={"content": {"sessions": sections}, "expected_version_no": 1},
            ).status_code
            == 409
        )
        sections[0]["title"] = "Tampered metadata"
        assert (
            client.patch(
                f"/api/v1/summaries/{summary['id']}",
                headers=headers,
                json={"content": {"sessions": sections}},
            ).status_code
            == 422
        )
        whole = client.post(root + "/summaries", headers=headers, json={**body, "scope": "meeting"})
        assert not run(app, whole.json()["id"])
        assert {
            row["scope"] for row in client.get(root + "/summaries", headers=headers).json()
        } == {"meeting", "sessions"}


def test_summary_snapshots_text_and_names_before_worker_and_ignores_empty_next_session(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        rows = client.get(root + "/transcript", headers=headers).json()["segments"]
        client.patch(
            f"/api/v1/segments/{rows[0]['logical_segment_id']}",
            headers=headers,
            json={"text": "User edited before queue"},
        )
        job = client.post(
            root + "/summaries",
            headers=headers,
            json={
                "scope": "sessions",
                "mode": "custom",
                "prompt": "Tóm tắt",
                "output_language": "vi",
            },
        ).json()
        sections = client.get(root + "/sections", headers=headers).json()
        client.patch(
            root + f"/sections/{sections[0]['id']}",
            headers=headers,
            json={"title": "Renamed after queue"},
        )
        client.patch(
            f"/api/v1/segments/{rows[0]['logical_segment_id']}",
            headers=headers,
            json={"text": "User edited after queue"},
        )
        assert not run(app, job["id"])
        output = client.get(root + "/summaries", headers=headers).json()[0]["current_version"][
            "content"
        ]["sessions"]
        assert output[0]["title"] == "Kế hoạch"
        assert output[0]["content"]["content"] == "User edited before queue"


def test_pause_stop_without_resume_keeps_empty_pending_session_without_ai_fabrication(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            socket.receive_json()
            socket.send_bytes(b"\x00\x00" * 16000)
            socket.receive_json()
            socket.send_json({"type": "recording.pause", "new_section": True})
            socket.receive_json()
            socket.receive_json()
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["status"] == "completed"
        job = client.post(
            root + "/summaries",
            headers=headers,
            json={
                "scope": "sessions",
                "mode": "template",
                "template": "general",
                "output_language": "vi",
            },
        ).json()
        run(app, job["id"])
        sections = client.get(root + "/summaries", headers=headers).json()[0]["current_version"][
            "content"
        ]["sessions"]
        assert len(sections) == 2 and sections[1]["content"] is None
        assert app.state.llm_provider.inputs == ["Nội dung 1"]


def test_section_names_acl_and_cross_meeting_checks(tmp_path):
    app, sender, url, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "section-owner@example.com")
        commenter = _session(client, sender, "section-comment@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]), UUID(commenter["user"]["id"]))
        root, headers = f"/api/v1/meetings/{seed['meeting_id']}", _headers(owner)
        # Ensure old data without sessions can still generate a one-session result.
        job = client.post(
            root + "/summaries",
            headers=headers,
            json={
                "scope": "sessions",
                "mode": "custom",
                "prompt": "Tóm tắt",
                "output_language": "vi",
            },
        ).json()
        run(app, job["id"])
        assert (
            len(
                client.get(root + "/summaries", headers=headers).json()[0]["current_version"][
                    "content"
                ]["sessions"]
            )
            == 1
        )
        client.post(root + "/complete", headers=headers)
        row = client.get(root + "/sections", headers=headers).json()[0]
        endpoint = root + f"/sections/{row['id']}"
        assert client.get(root + "/sections", headers=_headers(commenter)).status_code == 200
        assert (
            client.patch(
                endpoint, headers=_headers(commenter), json={"title": "Denied"}
            ).status_code
            == 403
        )
        assert client.patch(endpoint, headers=headers, json={"title": " "}).status_code == 422
        assert client.patch(endpoint, headers=headers, json={"title": "X" * 256}).status_code == 422
        other = _create_realtime_meeting(client, owner["access_token"])
        assert (
            client.patch(
                f"/api/v1/meetings/{other}/sections/{row['id']}",
                headers=headers,
                json={"title": "Wrong meeting"},
            ).status_code
            == 404
        )
        assert (
            client.patch(endpoint, headers=headers, json={"title": "  Nội dung cũ  "}).json()[
                "title"
            ]
            == "Nội dung cũ"
        )


def test_reconnect_while_paused_preserves_pending_session_without_starting_it(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        url = f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            socket.send_bytes(b"\x00\x00" * 16000)
            socket.receive_json()
            socket.send_json({"type": "recording.pause", "new_section": True})
            socket.receive_json()
            socket.receive_json()
        with client.websocket_connect(url + "&start_paused=1") as socket:
            assert socket.receive_json()["paused"] is True
            rows = client.get(root + "/sections", headers=headers).json()
            assert len(rows) == 2 and not rows[-1]["is_started"]
            socket.send_json({"type": "recording.resume"})
            assert socket.receive_json()["type"] == "recording.resumed"
            assert client.get(root + "/sections", headers=headers).json()[-1]["is_started"]
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["status"] == "completed"


def test_session_summary_failure_does_not_publish_partial_results(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        app.state.llm_provider.fail_on = "Nội dung 2"
        job = client.post(
            root + "/summaries",
            headers=headers,
            json={
                "scope": "sessions",
                "mode": "custom",
                "prompt": "Tóm tắt",
                "output_language": "vi",
            },
        ).json()
        assert not run(app, job["id"])
        assert client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()["status"] == "failed"
        assert client.get(root + "/summaries", headers=headers).json() == []


def test_polishing_acceptance_keeps_session_boundaries_for_summary(tmp_path):
    from test_transcript_polish import Proofreader

    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        app.state.llm_provider = Proofreader()
        job = client.post(root + "/transcript/polish", headers=headers).json()["job"]
        run(app, job["id"])
        proposal = client.get(root + "/transcript/polish", headers=headers).json()
        accepted = client.post(
            root + f"/transcript/polish/{job['id']}/accept",
            headers=headers,
            json={
                "revision": 0,
                "segments": [
                    {"logical_segment_id": row["logical_segment_id"], "text": row["text"]}
                    for row in proposal["segments"]
                ],
            },
        )
        assert accepted.status_code == 200
        app.state.llm_provider = CapturingLLM()
        summary = client.post(
            root + "/summaries",
            headers=headers,
            json={
                "scope": "sessions",
                "mode": "custom",
                "prompt": "Tóm tắt",
                "output_language": "vi",
            },
        ).json()
        assert not run(app, summary["id"])
        assert app.state.llm_provider.inputs == ["Nội dung 1.", "Nội dung 2."]


def test_manual_merge_cannot_cross_session_boundary(tmp_path):
    from app.models.meeting_section import MeetingSection

    app, sender, url, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _session(client, sender, "merge-sections@example.com")
        seed = _seed(url, UUID(owner["user"]["id"]))

        async def boundaries():
            async with app.state.database.session() as session:
                session.add_all(
                    [
                        MeetingSection(
                            meeting_id=UUID(seed["meeting_id"]),
                            sequence=1,
                            title="Một",
                            start_ms=0,
                            end_ms=1000,
                            is_started=True,
                        ),
                        MeetingSection(
                            meeting_id=UUID(seed["meeting_id"]),
                            sequence=2,
                            title="Hai",
                            start_ms=1000,
                            end_ms=2000,
                            is_started=True,
                        ),
                    ]
                )
                await session.commit()

        asyncio.run(boundaries())
        result = client.post(
            "/api/v1/segments/merge",
            headers=_headers(owner),
            json={"first_segment_id": seed["first"], "second_segment_id": seed["second"]},
        )
        assert result.status_code == 422 and result.json()["code"] == "SEGMENTS_NOT_MERGEABLE"


def test_completed_meeting_cannot_be_reopened_for_another_session(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, owner = two_sections(client, app, sender)
        meeting_id = root.rsplit("/", 1)[-1]
        count = len(app.state.stt_provider.sessions)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            error = socket.receive_json()
            assert error["type"] == "error" and error["code"] == "MEETING_COMPLETED"
        assert len(app.state.stt_provider.sessions) == count


def test_acknowledged_plain_pause_preserves_section_and_can_later_split(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    provider = FakeProvider()
    app.state.stt_provider = provider
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        before = client.get(root + "/sections", headers=headers).json()[0]["id"]
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            socket.send_bytes(b"audio-chunk")
            assert socket.receive_json()["type"] == "transcript.partial"
            assert socket.receive_json()["type"] == "transcript.final"
            for _ in range(2):
                socket.send_json(
                    {"type": "recording.pause", "new_section": False, "acknowledge": True}
                )
                assert socket.receive_json() == {"type": "recording.paused"}
                sections = client.get(root + "/sections", headers=headers).json()
                assert len(sections) == 1 and sections[0]["id"] == before
                assert sections[0]["end_ms"] is None
            assert provider.stream.paused and not provider.stream.closed
            socket.send_bytes(b"ignored while paused")
            assert socket.receive_json()["type"] == "error"  # Paused audio is rejected, not transcribed.
            socket.send_json({"type": "recording.resume", "acknowledge": True})
            resumed = socket.receive_json()
            assert resumed["type"] == "recording.resumed" and resumed["new_section"] is False
            assert len(client.get(root + "/sections", headers=headers).json()) == 1
            socket.send_json({"type": "recording.pause", "new_section": True, "acknowledge": True})
            split = socket.receive_json()
            assert split["type"] == "meeting.sections" and len(split["sections"]) == 2
            assert split["sections"][0]["id"] == before
            assert not split["sections"][1]["is_started"]
            socket.send_json({"type": "recording.resume", "acknowledge": True})
            resumed = socket.receive_json()
            assert resumed["type"] == "recording.resumed" and resumed["new_section"] is True
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["status"] == "completed"


def test_reconnect_after_plain_pause_resumes_same_section_not_a_new_one(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        url = f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        with client.websocket_connect(url) as socket:
            socket.receive_json()
            socket.send_json({"type": "recording.pause", "new_section": False, "acknowledge": True})
            assert socket.receive_json()["type"] == "recording.paused"
        with client.websocket_connect(url + "&start_paused=1") as socket:
            assert socket.receive_json()["paused"] is True
            socket.send_json({"type": "recording.resume", "acknowledge": True})
            resumed = socket.receive_json()
            assert resumed["type"] == "recording.resumed" and resumed["new_section"] is False
            assert len(client.get(root + "/sections", headers=headers).json()) == 1
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["status"] == "completed"


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"recording.pause","new_section":false,"acknowledge":"yes"}',
        '{"type":"recording.resume","acknowledge":1}',
    ],
)
def test_pause_acknowledgement_option_requires_a_boolean(payload):
    from app.api.v1.routes.realtime import _command

    assert _command(payload) is None
