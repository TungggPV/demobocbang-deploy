import asyncio
import json
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.meeting import Meeting
from app.models.transcript import SegmentVersion, TranscriptVersion
from app.providers.contracts import (
    StreamingOptions,
    StreamingTranscriptionEvent,
    STTCapabilities,
    STTProviderError,
    TranscriptionSegment,
    TranslationCapabilities,
    TranslationProviderError,
)
from app.providers.speechmatics import SpeechmaticsProvider


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_otp(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


class FakeStream:
    def __init__(self, error: STTProviderError | None = None) -> None:
        self.error = error
        self.paused = False
        self.resumed = False
        self.closed = False

    async def send_audio(self, chunk: bytes) -> tuple[StreamingTranscriptionEvent, ...]:
        if self.error:
            raise self.error
        assert chunk == b"audio-chunk"
        return (
            StreamingTranscriptionEvent(
                type="partial",
                language="vi",
                segment=TranscriptionSegment(0, 800, "Bản nháp không được lưu"),
            ),
            StreamingTranscriptionEvent(
                type="final",
                language="vi",
                segment=TranscriptionSegment(
                    0, 900, "Bản cuối được lưu", speaker_label="speaker_1", confidence=0.97
                ),
            ),
        )

    async def pause(self) -> tuple[StreamingTranscriptionEvent, ...]:
        self.paused = True
        return ()

    async def resume(self) -> tuple[StreamingTranscriptionEvent, ...]:
        self.resumed = True
        return ()

    async def close(self) -> tuple[StreamingTranscriptionEvent, ...]:
        self.closed = True
        return ()


class PushStream(FakeStream):
    """Output arrives after ready/pause even if the browser sends no audio."""

    def __init__(self) -> None:
        super().__init__()
        self.output = asyncio.Queue()
        self.output.put_nowait((StreamingTranscriptionEvent(
            "partial", "vi", TranscriptionSegment(0, 500, "Đang nói"),
            stream_segment_id="turn:0",
        ),))
        self.readers = 0
        self.close_events = ()

    async def receive_events(self):
        self.readers += 1
        try:
            return await self.output.get()
        finally:
            self.readers -= 1

    async def pause(self):
        self.paused = True
        self.output.put_nowait((StreamingTranscriptionEvent(
            "final", "vi", TranscriptionSegment(0, 900, "Đã nói xong"),
            stream_segment_id="turn:0",
        ),))
        return ()

    async def close(self):
        self.closed = True
        return self.close_events


class FakeProvider:
    def __init__(self, *, streaming: bool = True, error: STTProviderError | None = None) -> None:
        self.streaming = streaming
        self.stream = FakeStream(error)
        self.options: StreamingOptions | None = None

    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(
            batch=True,
            streaming=self.streaming,
            diarization=True,
            timestamps=True,
            confidence=True,
            supported_languages=frozenset({"vi", "en"}),
            multilingual_auto_detect=True,
        )

    async def transcribe_file(
        self, media_key: str, language: str | None = None
    ):  # pragma: no cover - protocol completeness
        raise NotImplementedError

    async def open_stream(self, options: StreamingOptions) -> FakeStream:
        self.options = options
        return self.stream


def test_realtime_pushes_partial_and_paused_final_without_new_audio(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    provider = FakeProvider()
    provider.stream = PushStream()
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            partial = websocket.receive_json()
            assert partial["type"] == "transcript.partial"
            assert partial["stream_segment_id"].endswith(":turn:0")
            assert partial["backend_queue_ms"] >= 0
            websocket.send_text('{"type":"recording.pause"}')
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["text"] == "Đã nói xong"
            assert final["stream_segment_id"] == partial["stream_segment_id"]
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json()["type"] == "meeting.status"
    assert provider.stream.readers == 0
    assert provider.stream.closed


def test_realtime_disconnect_flushes_pending_final_before_database_session_closes(
    tmp_path: Path,
) -> None:
    app, sender, _ = _app(tmp_path)
    provider = FakeProvider()
    provider.stream = PushStream()
    provider.stream.close_events = (StreamingTranscriptionEvent(
        "final", "vi", TranscriptionSegment(0, 900, "Lời cuối trước ngắt kết nối"),
        stream_segment_id="turn:0",
    ),)
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        # TestClient cancels the ASGI task on exit; the cancellation propagates
        # after the shielded flush. Still verify that the final was persisted.
        with suppress(FutureCancelledError), client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            assert websocket.receive_json()["type"] == "transcript.partial"
        response = client.get(
            f"/api/v1/meetings/{meeting_id}/transcript",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert response.status_code == 200
        assert [segment["text"] for segment in response.json()["segments"]] == [
            "Lời cuối trước ngắt kết nối"
        ]
    assert provider.stream.readers == 0


def test_realtime_push_provider_failure_is_reported_without_audio(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    provider = FakeProvider()
    provider.stream = PushStream()

    async def fail():
        raise STTProviderError("Provider disconnected.", retryable=True)

    provider.stream.receive_events = fail
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            error = websocket.receive_json()
            assert error["type"] == "error" and error["retryable"] is True
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1013


class FakeTranslation:
    def __init__(self, error: TranslationProviderError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[tuple[TranscriptionSegment, ...], str, str]] = []

    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(frozenset({("vi", "en"), ("en", "vi")}))

    async def translate_segments(
        self,
        segments: tuple[TranscriptionSegment, ...],
        source: str,
        target: str,
    ) -> tuple[TranscriptionSegment, ...]:
        self.calls.append((segments, source, target))
        if self.error:
            raise self.error
        return tuple(
            TranscriptionSegment(
                item.start_ms,
                item.end_ms,
                f"Translated: {item.text}",
                item.speaker_label,
                item.confidence,
            )
            for item in segments
        )


def test_realtime_persists_finals_but_never_partials_and_handles_pause_resume(
    tmp_path: Path,
) -> None:
    app, sender, database_url = _app(tmp_path)
    provider = FakeProvider()
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_text('{"type":"recording.pause"}')
            websocket.send_text('{"type":"recording.resume"}')
            websocket.send_bytes(b"audio-chunk")
            assert websocket.receive_json()["type"] == "transcript.partial"
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["text"] == "Bản cuối được lưu"
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as session:
            versions = list(await session.scalars(select(TranscriptVersion)))
            segments = list(await session.scalars(select(SegmentVersion)))
            meeting = (await session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert len(versions) == 1
        assert len(segments) == 1
        assert segments[0].text == "Bản cuối được lưu"
        assert meeting.status == "completed"

    asyncio.run(verify())
    assert provider.options is not None and provider.options.diarization is True
    assert provider.stream.paused and provider.stream.resumed and provider.stream.closed


@pytest.mark.parametrize("final_end", [1.2, 1.1])
def test_speechmatics_fragments_update_one_row_and_persist_one_segment(
    tmp_path: Path, final_end: float,
) -> None:
    class FragmentedVoiceClient:
        def __init__(self, **kwargs):
            self.handlers = {}
            self.fragments = iter([
                ("AddPartialSegment", 0, .3, "Xin"),
                ("AddSegment", 0, .3, "Xin"),
                ("AddPartialSegment", .35, .65, "chào"),
                ("AddSegment", .35, .65, "chào"),
                ("AddPartialSegment", .7, 1.2, "mọi người"),
                ("AddSegment", .7, final_end, "mọi người."),
            ])

        def on(self, event, callback):
            self.handlers[event.value] = callback

        async def connect(self):
            pass

        async def send_audio(self, chunk):
            event, start, end, text = next(self.fragments)
            self.handlers[event]({"segments": [{
                "speaker_id": "S1", "text": text,
                "is_eou": text == "mọi người.",
                "metadata": {"start_time": start, "end_time": end},
            }]})

        async def disconnect(self):
            pass

    app, sender, _ = _app(tmp_path)
    app.state.stt_provider = SpeechmaticsProvider(
        Settings(_env_file=None, speechmatics_api_key="test-key",
                 speechmatics_finalization_delay_seconds=3),
        voice_client_factory=FragmentedVoiceClient,
    )
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        token = session["access_token"]
        meeting_id = _create_realtime_meeting(client, token)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={token}"
        ) as websocket:
            ready = websocket.receive_json()
            assert ready["type"] == "session.ready" and ready["provider"] == "speechmatics"
            previews = []
            for _ in range(6):
                websocket.send_bytes(b"audio-chunk")
                event = websocket.receive_json()
                assert event["type"] == "transcript.partial"
                previews.append(event)
            assert previews[2]["text"] == "Xin chào"
            assert previews[4]["text"] == "Xin chào mọi người"
            assert len({event["stream_segment_id"] for event in previews}) == 1
            websocket.send_json({"type": "recording.stop"})
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["text"] == "Xin chào mọi người."
            assert final["stream_end_ms"] == round(final_end * 1000)
            assert final["stream_segment_id"] == previews[0]["stream_segment_id"]
            assert websocket.receive_json()["type"] == "meeting.status"
        response = client.get(
            f"/api/v1/meetings/{meeting_id}/transcript",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        segments = response.json()["segments"]
        assert len(segments) == 1
        assert segments[0]["text"] == "Xin chào mọi người."
        assert segments[0]["end_ms"] == round(final_end * 1000)
        assert segments[0]["logical_segment_id"] == final["logical_segment_id"]


def test_realtime_translation_only_translates_final_segments(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    provider, translation = FakeProvider(), FakeTranslation()
    app.state.stt_provider = provider
    app.state.translation_provider = translation
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_text(
                '{"type":"translation.enable","target_language":"en",'
                '"logical_segment_ids":[]}'
            )
            assert websocket.receive_json() == {
                "type": "translation.status",
                "enabled": True,
                "source_language": "vi",
                "target_language": "en",
            }
            websocket.send_bytes(b"audio-chunk")
            assert websocket.receive_json()["type"] == "transcript.partial"
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            translated = websocket.receive_json()
            assert translated == {
                "type": "translation.final",
                "logical_segment_id": final["logical_segment_id"],
                "source_language": "vi",
                "target_language": "en",
                "text": "Translated: Bản cuối được lưu",
            }
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}

    assert len(translation.calls) == 1
    segments, source, target = translation.calls[0]
    assert [item.text for item in segments] == ["Bản cuối được lưu"]
    assert (source, target) == ("vi", "en")


def test_realtime_translation_disable_and_reenable_backfills_without_duplicates(
    tmp_path: Path,
) -> None:
    app, sender, _ = _app(tmp_path)
    translation = FakeTranslation()
    app.state.stt_provider = FakeProvider()
    app.state.translation_provider = translation
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"audio-chunk")
            websocket.receive_json()
            first_final = websocket.receive_json()
            assert translation.calls == []

            websocket.send_text(
                json.dumps(
                    {
                        "type": "translation.enable",
                        "target_language": "en",
                        "logical_segment_ids": [first_final["logical_segment_id"]],
                    }
                )
            )
            assert websocket.receive_json()["type"] == "translation.status"
            assert (
                websocket.receive_json()["logical_segment_id"]
                == first_final["logical_segment_id"]
            )

            websocket.send_text('{"type":"translation.disable"}')
            assert websocket.receive_json() == {"type": "translation.status", "enabled": False}
            websocket.send_bytes(b"audio-chunk")
            websocket.receive_json()
            second_final = websocket.receive_json()

            websocket.send_text(
                json.dumps(
                    {
                        "type": "translation.enable",
                        "target_language": "en",
                        "logical_segment_ids": [
                            first_final["logical_segment_id"],
                            second_final["logical_segment_id"],
                        ],
                    }
                )
            )
            assert websocket.receive_json()["type"] == "translation.status"
            assert (
                websocket.receive_json()["logical_segment_id"]
                == second_final["logical_segment_id"]
            )
            websocket.send_text('{"type":"recording.stop"}')
            websocket.receive_json()

    assert len(translation.calls) == 2
    assert all(len(segments) == 1 for segments, _, _ in translation.calls)


def test_realtime_translation_errors_do_not_interrupt_stt(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    translation = FakeTranslation(
        TranslationProviderError("Translator unavailable.", code="TRANSLATION_DOWN")
    )
    app.state.stt_provider = FakeProvider()
    app.state.translation_provider = translation
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_text(
                '{"type":"translation.enable","target_language":"fr",'
                '"logical_segment_ids":[]}'
            )
            unsupported = websocket.receive_json()
            assert unsupported["type"] == "translation.error"
            assert unsupported["code"] == "PROVIDER_CAPABILITY_UNAVAILABLE"
            assert translation.calls == []

            websocket.send_text(
                json.dumps(
                    {
                        "type": "translation.enable",
                        "target_language": "en",
                        "logical_segment_ids": [str(uuid4())],
                    }
                )
            )
            invalid = websocket.receive_json()
            assert invalid["type"] == "translation.error"
            assert invalid["code"] == "TRANSLATION_SEGMENT_NOT_FOUND"
            assert translation.calls == []

            websocket.send_text(
                '{"type":"translation.enable","target_language":"en",'
                '"logical_segment_ids":[]}'
            )
            assert websocket.receive_json()["type"] == "translation.status"
            websocket.send_bytes(b"audio-chunk")
            assert websocket.receive_json()["type"] == "transcript.partial"
            assert websocket.receive_json()["type"] == "transcript.final"
            error = websocket.receive_json()
            assert error["type"] == "translation.error"
            assert error["code"] == "TRANSLATION_DOWN"

            websocket.send_text('{"type":"recording.pause"}')
            websocket.send_text('{"type":"recording.resume"}')
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}


def test_realtime_rejects_provider_without_streaming_capability(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    app.state.stt_provider = FakeProvider(streaming=False)
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            error = websocket.receive_json()
            assert error["code"] == "PROVIDER_CAPABILITY_UNAVAILABLE"
            assert error["retryable"] is False
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()


def test_realtime_retryable_provider_error_closes_for_reconnect_without_failing_meeting(
    tmp_path: Path,
) -> None:
    app, sender, database_url = _app(tmp_path)
    app.state.stt_provider = FakeProvider(
        error=STTProviderError("Temporary streaming interruption.", retryable=True)
    )
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"audio-chunk")
            error = websocket.receive_json()
            assert error["code"] == "STT_PROVIDER_ERROR"
            assert error["retryable"] is True
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1013

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as session:
            meeting = (await session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert meeting.status == "recording"

    asyncio.run(verify())

def test_realtime_non_retryable_provider_error_fails_meeting_not_completed(tmp_path: Path) -> None:
    app, sender, database_url = _app(tmp_path)
    app.state.stt_provider = FakeProvider(
        error=STTProviderError("Provider rejected the audio.", retryable=False)
    )
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"audio-chunk")
            error = websocket.receive_json()
            assert error == {
                "type": "error",
                "code": "STT_PROVIDER_ERROR",
                "message": "Provider rejected the audio.",
                "retryable": False,
            }

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as session:
            meeting = (await session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert meeting.status == "failed"

    asyncio.run(verify())


def _app(tmp_path: Path) -> tuple[object, CapturingEmailSender, str]:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'realtime.sqlite3').as_posix()}"

    async def create_schema() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create_schema())
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            jwt_secret="test-secret",
            frontend_origins="http://localhost:5173",
            stt_provider="fake",
        )
    )
    sender = CapturingEmailSender()
    app.state.email_sender = sender
    return app, sender, database_url


def _verified_session(client: TestClient, sender: CapturingEmailSender) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "speaker@example.com",
            "password": "correct horse battery",
            "full_name": "Speaker",
        },
    )
    assert response.status_code == 201
    verified = client.post(
        "/api/v1/auth/verify-email-otp",
        json={"email": "speaker@example.com", "code": sender.messages[-1][1]},
    )
    assert verified.status_code == 200
    return verified.json()


def _create_realtime_meeting(client: TestClient, token: object) -> str:
    response = client.post(
        "/api/v1/meetings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Realtime test",
            "source_type": "realtime",
            "language_mode": "vi",
            "diarization_enabled": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_realtime_saves_media_file_on_recording_complete(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    provider = FakeProvider()
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        token = session["access_token"]
        meeting_id = _create_realtime_meeting(client, token)
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={token}"
        ) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"audio-chunk")
            assert websocket.receive_json()["type"] == "transcript.partial"
            assert websocket.receive_json()["type"] == "transcript.final"
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}

        media_res = client.get(
            f"/api/v1/meetings/{meeting_id}/media",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert media_res.status_code == 200
        media_list = media_res.json()
        assert len(media_list) == 1
        media = media_list[0]
        assert media["mime_type"] == "audio/wav"
        assert media["status"] == "ready"
        assert media["size_bytes"] > 44

        stream_res = client.get(
            f"/api/v1/meetings/{meeting_id}/media/{media['id']}/stream",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert stream_res.status_code == 200
        assert stream_res.content[:4] == b"RIFF"
        assert stream_res.content[8:12] == b"WAVE"

