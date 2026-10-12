import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.media import MediaFile
from app.models.meeting import Meeting
from app.models.transcript import SegmentVersion, Speaker, TranscriptVersion
from app.providers.contracts import (
    BatchTranscriptionOptions,
    StreamingOptions,
    StreamingTranscriptionEvent,
    STTCapabilities,
    STTProviderError,
    TranscriptionSegment,
    TranslationCapabilities,
    TranslationProviderError,
)


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
        self.close_events: tuple[StreamingTranscriptionEvent, ...] = ()

    async def send_audio(self, chunk: bytes) -> tuple[StreamingTranscriptionEvent, ...]:
        if self.error:
            raise self.error
        assert isinstance(chunk, bytes) and chunk
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
                provider_segment_id="0",
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
        self, media_key: str, options: BatchTranscriptionOptions
    ):  # pragma: no cover - protocol completeness
        raise NotImplementedError

    async def open_stream(self, options: StreamingOptions) -> FakeStream:
        self.options = options
        return self.stream


class AutoLanguageStream(FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.turn = 0

    async def send_audio(self, chunk: bytes) -> tuple[StreamingTranscriptionEvent, ...]:
        language, text = (
            ("vi", "Xin chào"),
            ("en", "Good morning"),
            ("fr", "Bonjour"),
        )[self.turn]
        start_ms = self.turn * 1000
        self.turn += 1
        return (
            StreamingTranscriptionEvent(
                type="final",
                language=language,
                segment=TranscriptionSegment(start_ms, start_ms + 800, text),
                provider_segment_id=str(self.turn),
            ),
        )


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
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.partial"
            final = websocket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["text"] == "Bản cuối được lưu."
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}

        media_response = client.get(
            f"/api/v1/meetings/{meeting_id}/media",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert media_response.status_code == 200
        media = media_response.json()
        assert len(media) == 1
        assert media[0]["mime_type"] == "audio/wav"
        assert media[0]["status"] == "ready"
        playback_response = client.post(
            f"/api/v1/meetings/{meeting_id}/media/{media[0]['id']}/playback-url",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert playback_response.status_code == 200
        playback_url = urlsplit(playback_response.json()["url"])
        playback = client.get(
            playback_url.path,
            headers={"Range": "bytes=0-43"},
        )
        assert playback.status_code == 206
        assert playback.headers["content-type"] == "audio/wav"

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
        assert segments[0].text == "Bản cuối được lưu."
        assert meeting.status == "completed"

    asyncio.run(verify())
    assert provider.options is not None and provider.options.diarization is True
    assert provider.stream.paused and provider.stream.resumed and provider.stream.closed


def test_realtime_continue_appends_transcript_timeline_and_recording(tmp_path: Path) -> None:
    class InterruptingStream(FakeStream):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def send_audio(
            self, chunk: bytes
        ) -> tuple[StreamingTranscriptionEvent, ...]:
            self.calls += 1
            if self.calls > 1:
                raise STTProviderError("Temporary interruption.", retryable=True)
            return await super().send_audio(chunk)

    class ShortStream(FakeStream):
        async def send_audio(
            self, chunk: bytes
        ) -> tuple[StreamingTranscriptionEvent, ...]:
            assert isinstance(chunk, bytes) and chunk
            return (
                StreamingTranscriptionEvent(
                    type="partial",
                    language="vi",
                    segment=TranscriptionSegment(0, 80, "Đoạn tiếp theo"),
                ),
                StreamingTranscriptionEvent(
                    type="final",
                    language="vi",
                    segment=TranscriptionSegment(0, 100, "Đoạn tiếp theo"),
                    provider_segment_id="2",
                ),
            )

    app, sender, database_url = _app(tmp_path)
    first_provider = FakeProvider()
    first_provider.stream = InterruptingStream()
    first_provider.stream.close_events = (
        StreamingTranscriptionEvent(
            type="final",
            language="vi",
            segment=TranscriptionSegment(900, 1000, "Đoạn chốt khi ngắt kết nối"),
            provider_segment_id="1",
        ),
    )
    app.state.stt_provider = first_provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        socket_url = (
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        )
        with client.websocket_connect(socket_url) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.partial"
            first = websocket.receive_json()
            assert (first["start_ms"], first["end_ms"]) == (0, 900)
            websocket.send_bytes(b"\x00\x00" * 1600)
            interrupted = websocket.receive_json()
            assert interrupted["type"] == "error"
            assert interrupted["retryable"] is True
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

        for _ in range(100):
            persisted_media = client.get(
                f"/api/v1/meetings/{meeting_id}/media",
                headers={"Authorization": f"Bearer {session['access_token']}"},
            ).json()
            persisted_transcript = client.get(
                f"/api/v1/meetings/{meeting_id}/transcript",
                headers={"Authorization": f"Bearer {session['access_token']}"},
            )
            if persisted_media and len(persisted_transcript.json().get("segments", [])) == 2:
                break
            time.sleep(0.01)
        assert persisted_media
        assert len(persisted_transcript.json()["segments"]) == 2

        async def make_duration_metadata_stale() -> None:
            engine = create_async_engine(database_url)
            session_factory = async_sessionmaker(engine)
            async with session_factory() as database_session:
                meeting = (await database_session.scalars(select(Meeting))).one()
                media = (await database_session.scalars(select(MediaFile))).one()
                meeting.duration_ms = 200
                media.duration_ms = 200
                await database_session.commit()
            await engine.dispose()

        asyncio.run(make_duration_metadata_stale())

        second_provider = FakeProvider()
        second_provider.stream = ShortStream()
        app.state.stt_provider = second_provider
        with client.websocket_connect(socket_url) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"\x00\x00" * 1600)
            partial = websocket.receive_json()
            assert partial["type"] == "transcript.partial", partial
            second = websocket.receive_json()
            assert (second["start_ms"], second["end_ms"]) == (1000, 1100)
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {
                "type": "meeting.status",
                "status": "completed",
            }

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as database_session:
            segments = list(
                await database_session.scalars(
                    select(SegmentVersion).order_by(SegmentVersion.sequence)
                )
            )
            media = list(await database_session.scalars(select(MediaFile)))
            meeting = (await database_session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert [(item.start_ms, item.end_ms) for item in segments] == [
            (0, 900),
            (900, 1000),
            (1000, 1100),
        ]
        assert len(media) == 1
        assert media[0].duration_ms == 1100
        assert meeting.duration_ms == 1100

    asyncio.run(verify())


def test_realtime_can_finish_existing_recording_without_reopening_microphone(
    tmp_path: Path,
) -> None:
    app, sender, database_url = _app(tmp_path)
    app.state.stt_provider = FakeProvider()
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        socket_url = (
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        )
        with client.websocket_connect(socket_url) as websocket:
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"\x00\x00" * 1600)
            assert websocket.receive_json()["type"] == "transcript.partial"
            assert websocket.receive_json()["type"] == "transcript.final"

        response = client.post(
            f"/api/v1/meetings/{meeting_id}/complete",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

        with client.websocket_connect(socket_url) as websocket:
            completed = websocket.receive_json()
            assert completed["code"] == "MEETING_ALREADY_COMPLETED"

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as database_session:
            meeting = (await database_session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert meeting.status == "completed"

    asyncio.run(verify())


def test_realtime_applies_final_speaker_revision_before_completion(tmp_path: Path) -> None:
    app, sender, database_url = _app(tmp_path)
    provider = FakeProvider()
    provider.stream.close_events = (
        StreamingTranscriptionEvent(
            type="speaker_revision",
            language="vi",
            segment=TranscriptionSegment(
                0, 900, "Bản cuối được lưu", speaker_label="speaker_2"
            ),
            provider_segment_id="0",
        ),
    )
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"audio-chunk")
            websocket.receive_json()
            final = websocket.receive_json()
            websocket.send_text('{"type":"recording.stop"}')
            revision = websocket.receive_json()
            assert revision == {
                "type": "transcript.speaker_revision",
                "logical_segment_id": final["logical_segment_id"],
                "speaker_label": "speaker_2",
            }
            assert websocket.receive_json() == {
                "type": "meeting.status",
                "status": "completed",
            }

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as database_session:
            speaker_label = await database_session.scalar(
                select(Speaker.provider_speaker_id)
                .join(SegmentVersion, SegmentVersion.speaker_id == Speaker.id)
            )
        await engine.dispose()
        assert speaker_label == "speaker_2"

    asyncio.run(verify())


def test_realtime_silence_fails_instead_of_reporting_success(tmp_path: Path) -> None:
    class SilentStream(FakeStream):
        async def send_audio(
            self, chunk: bytes
        ) -> tuple[StreamingTranscriptionEvent, ...]:
            assert chunk == b"audio-chunk"
            return ()

    app, sender, database_url = _app(tmp_path)
    provider = FakeProvider()
    provider.stream = SilentStream()
    app.state.stt_provider = provider
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"audio-chunk")
            websocket.send_text('{"type":"recording.stop"}')
            error = websocket.receive_json()
            assert error["code"] == "STT_NO_SPEECH"
            assert error["retryable"] is False

    async def verify() -> None:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine)
        async with session_factory() as database_session:
            meeting = (await database_session.scalars(select(Meeting))).one()
        await engine.dispose()
        assert meeting.status == "failed"

    asyncio.run(verify())


def test_realtime_rejects_upload_meetings_and_completed_sessions(tmp_path: Path) -> None:
    app, sender, _ = _app(tmp_path)
    app.state.stt_provider = FakeProvider()
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        upload_meeting_id = _create_realtime_meeting(
            client, session["access_token"], source_type="upload"
        )
        with client.websocket_connect(
            f"/api/v1/realtime/{upload_meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            mismatch = websocket.receive_json()
            assert mismatch["code"] == "MEETING_SOURCE_TYPE_MISMATCH"

        meeting_id = _create_realtime_meeting(client, session["access_token"])
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"audio-chunk")
            websocket.receive_json()
            websocket.receive_json()
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json()["status"] == "completed"
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            completed = websocket.receive_json()
            assert completed["code"] == "MEETING_ALREADY_COMPLETED"


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
                "text": "Translated: Bản cuối được lưu.",
            }
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {"type": "meeting.status", "status": "completed"}

    assert len(translation.calls) == 1
    segments, source, target = translation.calls[0]
    assert [item.text for item in segments] == ["Bản cuối được lưu."]
    assert (source, target) == ("vi", "en")


def test_realtime_auto_detect_translates_only_between_vietnamese_and_english(
    tmp_path: Path,
) -> None:
    app, sender, _ = _app(tmp_path)
    provider, translation = FakeProvider(), FakeTranslation()
    provider.stream = AutoLanguageStream()
    app.state.stt_provider = provider
    app.state.translation_provider = translation
    with TestClient(app) as client:
        session = _verified_session(client, sender)
        meeting_id = _create_realtime_meeting(
            client, session["access_token"], language_mode="auto"
        )
        with client.websocket_connect(
            f"/api/v1/realtime/{meeting_id}?access_token={session['access_token']}"
        ) as websocket:
            ready = websocket.receive_json()
            assert ready["language"] == "auto"
            assert provider.options is not None and provider.options.language == "auto"
            websocket.send_text(
                '{"type":"translation.enable","target_language":"auto",'
                '"logical_segment_ids":[]}'
            )
            assert websocket.receive_json() == {
                "type": "translation.status",
                "enabled": True,
                "source_language": "auto",
                "target_language": "auto",
            }

            websocket.send_bytes(b"vietnamese")
            vi_final = websocket.receive_json()
            vi_translation = websocket.receive_json()
            assert vi_final["language"] == "vi"
            assert vi_translation["source_language"] == "vi"
            assert vi_translation["target_language"] == "en"

            websocket.send_bytes(b"english")
            en_final = websocket.receive_json()
            en_translation = websocket.receive_json()
            assert en_final["language"] == "en"
            assert en_translation["source_language"] == "en"
            assert en_translation["target_language"] == "vi"

            websocket.send_bytes(b"other-language")
            other_final = websocket.receive_json()
            assert other_final["language"] == "fr"
            websocket.send_text('{"type":"recording.stop"}')
            assert websocket.receive_json() == {
                "type": "meeting.status",
                "status": "completed",
            }

    assert [(source, target) for _, source, target in translation.calls] == [
        ("vi", "en"),
        ("en", "vi"),
    ]


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
            media_storage_path=tmp_path / "objects",
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


def _create_realtime_meeting(
    client: TestClient,
    token: object,
    *,
    source_type: str = "realtime",
    language_mode: str = "vi",
) -> str:
    response = client.post(
        "/api/v1/meetings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": "Realtime test",
            "source_type": source_type,
            "language_mode": language_mode,
            "diarization_enabled": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]
