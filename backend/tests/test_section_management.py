"""Session management must never mutate the meeting's actual content."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_meeting_sections import setup, two_sections
from test_realtime import _create_realtime_meeting, _verified_session
from test_transcript_polish import run
from test_transcripts import _headers, _session

from app.db.base import Base


def layout(client, root, headers):
    response = client.get(root + "/sections", headers=headers)
    assert response.status_code == 200
    return response.json()


def add(client, root, headers, start, title="Phiên thêm"):
    rows = layout(client, root, headers)
    return client.post(
        root + "/sections",
        headers=headers,
        json={
            "title": title,
            "start_ms": start,
            "expected_revision": rows[0]["revision"] if rows else "",
        },
    )


def remove(client, root, headers, row):
    return client.delete(
        root + f"/sections/{row['id']}",
        headers=headers,
        params={"expected_revision": row["revision"]},
    )


def content_snapshot(url):
    async def read():
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            result = {}
            for name, table in Base.metadata.tables.items():
                if name != "meeting_sections":
                    result[name] = sorted(
                        repr(tuple(row)) for row in (await connection.execute(select(table))).all()
                    )
        await engine.dispose()
        return result

    return asyncio.run(read())


def test_live_grouping_edits_preserve_pending_resume_and_recording(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        owner = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, owner["access_token"])
        root, headers = f"/api/v1/meetings/{meeting_id}", _headers(owner)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={owner['access_token']}"
        ) as socket:
            assert socket.receive_json()["type"] == "session.ready"
            socket.send_bytes(b"\x00\x00" * 16000)
            assert socket.receive_json()["type"] == "transcript.partial"
            socket.send_json({"type": "recording.pause", "new_section": True})
            assert socket.receive_json()["type"] == "transcript.final"
            assert socket.receive_json()["type"] == "meeting.sections"
            assert add(client, root, headers, 500).status_code == 201
            rows = layout(client, root, headers)
            assert len(rows) == 3 and rows[-1]["is_started"] is False
            # Deleting the pending grouping must not stop, lose audio or force a new session.
            assert remove(client, root, headers, rows[-1]).status_code == 200
            socket.send_json({"type": "recording.resume", "acknowledge": True})
            resumed = socket.receive_json()
            assert resumed["type"] == "recording.resumed" and resumed["new_section"] is False
            socket.send_bytes(b"\x00\x00" * 16000)
            assert socket.receive_json()["type"] == "transcript.partial"
            socket.send_json({"type": "recording.stop"})
            assert socket.receive_json()["type"] == "transcript.final"
            assert socket.receive_json()["status"] == "completed"
        rows = layout(client, root, headers)
        assert [(row["start_ms"], row["end_ms"]) for row in rows] == [(0, 500), (500, 2000)]
        assert [
            row["text"]
            for row in client.get(root + "/transcript", headers=headers).json()["segments"]
        ] == ["Nội dung 1", "Nội dung 2"]


def test_rename_zero_length_pending_at_end_and_initialize_legacy_sessions(tmp_path):
    from uuid import UUID

    from test_transcripts import _seed

    app, sender, url, _ = setup(tmp_path)
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
        rows = layout(client, root, headers)
        pending = rows[-1]
        assert pending["start_ms"] == pending["end_ms"] == 1000
        response = client.patch(
            root + f"/sections/{pending['id']}",
            headers=headers,
            json={
                "title": "Chưa bàn",
                "start_ms": 1000,
                "end_ms": 1000,
                "expected_revision": pending["revision"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_started"] is False
        rows = layout(client, root, headers)
        # Moving the preceding boundary backwards gives the former empty session real content.
        assert (
            client.patch(
                root + f"/sections/{rows[0]['id']}",
                headers=headers,
                json={"end_ms": 700, "expected_revision": rows[0]["revision"]},
            ).status_code
            == 200
        )
        assert layout(client, root, headers)[-1]["is_started"] is True
        seed = _seed(url, UUID(owner["user"]["id"]))
        legacy_root = f"/api/v1/meetings/{seed['meeting_id']}"
        before = client.get(legacy_root + "/transcript", headers=headers).json()
        assert layout(client, legacy_root, headers) == []
        assert add(client, legacy_root, headers, 0).status_code == 201
        assert add(client, legacy_root, headers, 500).status_code == 201
        assert client.get(legacy_root + "/transcript", headers=headers).json() == before


def test_add_edit_delete_only_changes_grouping_and_keeps_summary_history(tmp_path):
    app, sender, url, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
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
        before = content_snapshot(url)
        original = client.get(root + "/transcript", headers=headers).json()
        old_summary = client.get(root + "/summaries", headers=headers).json()
        response = add(client, root, headers, 500, "  Thảo luận  ")
        assert response.status_code == 201, response.text
        rows = response.json()
        assert [(row["sequence"], row["start_ms"], row["end_ms"]) for row in rows] == [
            (1, 0, 500),
            (2, 500, 1000),
            (3, 1000, 2000),
        ]
        assert rows[1]["title"] == "Thảo luận"
        response = client.patch(
            root + f"/sections/{rows[1]['id']}",
            headers=headers,
            json={
                "title": "Đã chỉnh",
                "start_ms": 400,
                "end_ms": 1200,
                "expected_revision": rows[0]["revision"],
            },
        )
        assert response.status_code == 200, response.text
        rows = layout(client, root, headers)
        assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
            (0, 400),
            (400, 1200),
            (1200, 2000),
        ]
        assert remove(client, root, headers, rows[1]).status_code == 200
        rows = layout(client, root, headers)
        assert [(row["start_ms"], row["end_ms"]) for row in rows] == [(0, 1200), (1200, 2000)]
        assert remove(client, root, headers, rows[0]).status_code == 200  # First merges forward.
        rows = layout(client, root, headers)
        assert len(rows) == 1 and rows[0]["start_ms"] == 0 and rows[0]["end_ms"] == 2000
        assert remove(client, root, headers, rows[0]).status_code == 422
        assert client.get(root + "/transcript", headers=headers).json() == original
        assert client.get(root + "/summaries", headers=headers).json() == old_summary
        assert (
            content_snapshot(url) == before
        )  # Includes all transcript versions, speakers, chunks, media and jobs.


@pytest.mark.parametrize("mode", ["custom", "template"])
def test_selected_summary_groups_all_boundaries_before_filtering_and_snapshots(tmp_path, mode):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        rows = layout(client, root, headers)
        body = {
            "scope": "sessions",
            "mode": mode,
            "output_language": "vi",
            "section_ids": [rows[1]["id"]],
            "sections_revision": rows[0]["revision"],
        }
        body.update({"prompt": "Tóm tắt"} if mode == "custom" else {"template": "general"})
        queued = client.post(root + "/summaries", headers=headers, json=body)
        assert queued.status_code == 202, queued.text
        assert remove(client, root, headers, rows[1]).status_code == 200
        assert not run(app, queued.json()["id"])
        assert app.state.llm_provider.inputs == ["Nội dung 2"]
        summary = client.get(root + "/summaries", headers=headers).json()[0]
        result = summary["current_version"]["content"]["sessions"]
        assert (
            len(result) == 1
            and result[0]["id"] == rows[1]["id"]
            and result[0]["title"] == "Ngân sách"
        )
        # Regeneration with a different subset is a new immutable version, not an overwrite.
        rows = layout(client, root, headers)
        queued = client.post(
            root + "/summaries",
            headers=headers,
            json={**body, "section_ids": [rows[0]["id"]], "sections_revision": rows[0]["revision"]},
        )
        assert not run(app, queued.json()["id"])
        versions = client.get(f"/api/v1/summaries/{summary['id']}/versions", headers=headers).json()
        assert [version["version_no"] for version in versions] == [2, 1]
        assert versions[-1]["content"]["sessions"] == result


def test_reject_invalid_selections_stale_layout_and_cross_meeting_ids(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, owner = two_sections(client, app, sender)
        rows = layout(client, root, headers)
        other = _create_realtime_meeting(client, owner["access_token"])
        foreign = layout(client, f"/api/v1/meetings/{other}", headers)[0]
        body = {"scope": "sessions", "mode": "custom", "prompt": "Tóm tắt", "output_language": "vi"}
        for ids in [[], [rows[0]["id"], rows[0]["id"]], [foreign["id"]], [str(uuid4())]]:
            assert (
                client.post(
                    root + "/summaries", headers=headers, json={**body, "section_ids": ids}
                ).status_code
                == 422
            )
        assert (
            client.post(
                root + "/summaries",
                headers=headers,
                json={**body, "scope": "meeting", "section_ids": [rows[0]["id"]]},
            ).status_code
            == 422
        )
        assert add(client, root, headers, 500).status_code == 201
        assert (
            client.post(
                root + "/summaries",
                headers=headers,
                json={
                    **body,
                    "section_ids": [rows[0]["id"]],
                    "sections_revision": rows[0]["revision"],
                },
            ).status_code
            == 409
        )
        assert remove(client, root, headers, rows[1]).status_code == 409
        assert (
            client.patch(
                root + f"/sections/{rows[0]['id']}",
                headers=headers,
                json={"end_ms": 800, "expected_revision": rows[0]["revision"]},
            ).status_code
            == 409
        )
        assert app.state.llm_provider.inputs == []


@pytest.mark.parametrize("start", [-1, 0, 1000, 2000, 3000, True, 0.5])
def test_invalid_creation_never_changes_content_or_layout(tmp_path, start):
    app, sender, url, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        rows, before = layout(client, root, headers), content_snapshot(url)
        assert add(client, root, headers, start).status_code == 422
        assert layout(client, root, headers) == rows
        assert content_snapshot(url) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"start_ms": 1},
        {"end_ms": None},
        {"end_ms": 0},
        {"end_ms": 2001},
        {"start_ms": None},
        {"end_ms": True},
    ],
)
def test_invalid_boundary_edits_leave_layout_untouched(tmp_path, changes):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        rows = layout(client, root, headers)
        response = client.patch(
            root + f"/sections/{rows[0]['id']}",
            headers=headers,
            json={**changes, "expected_revision": rows[0]["revision"]},
        )
        assert response.status_code == 422, response.text
        assert layout(client, root, headers) == rows


def test_acl_and_wrong_meeting_mutations(tmp_path):
    app, sender, _, _ = setup(tmp_path)
    with TestClient(app) as client:
        root, headers, _ = two_sections(client, app, sender)
        member = _session(client, sender, "section-viewer@example.com")
        client.post(
            root + "/members",
            headers=headers,
            json={"email": "section-viewer@example.com", "permission": "comment"},
        )
        rows = layout(client, root, _headers(member))
        assert (
            client.post(
                root + "/sections",
                headers=_headers(member),
                json={"title": "Không", "start_ms": 500, "expected_revision": rows[0]["revision"]},
            ).status_code
            == 403
        )
        assert (
            client.patch(
                root + f"/sections/{rows[0]['id']}",
                headers=_headers(member),
                json={"title": "Không"},
            ).status_code
            == 403
        )
        assert remove(client, root, _headers(member), rows[0]).status_code == 403
        owner = _session(client, sender, "other-section-owner@example.com")
        other = _create_realtime_meeting(client, owner["access_token"])
        other_root = f"/api/v1/meetings/{other}"
        foreign = layout(client, other_root, _headers(owner))[0]
        assert (
            remove(
                client, other_root, _headers(owner), {**foreign, "id": rows[0]["id"]}
            ).status_code
            == 404
        )
