"""AssemblyAI adapters mapped to the application's STT contracts.

The batch API receives a normalized WAV uploaded by the media worker.  The
streaming API is deliberately server-to-server: AssemblyAI credentials never
reach the browser.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.providers.contracts import (
    BatchTranscriptionOptions,
    StreamingOptions,
    StreamingTranscriptionEvent,
    STTCapabilities,
    STTProviderError,
    TranscriptionResult,
    TranscriptionSegment,
)


class AssemblyAIProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.stt_api_key:
            raise ValueError("STT_API_KEY is required when STT_PROVIDER=assemblyai.")
        self._key = settings.stt_api_key
        self._model = settings.stt_model
        self._fallback_model = settings.stt_fallback_model
        self._api_base_url = settings.assemblyai_api_base_url.rstrip("/")
        self._streaming_base_url = settings.assemblyai_streaming_base_url.rstrip("/")
        self._streaming_model = settings.assemblyai_streaming_model
        self._streaming_mode = settings.assemblyai_streaming_mode
        self._streaming_min_turn_silence_ms = (
            settings.assemblyai_streaming_min_turn_silence_ms
        )
        self._streaming_max_turn_silence_ms = (
            settings.assemblyai_streaming_max_turn_silence_ms
        )
        self._poll_interval = settings.stt_poll_interval_seconds
        self._poll_timeout = settings.stt_poll_timeout_seconds
        self._client = client

    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(True, True, True, True, True, frozenset({"vi", "en"}), True)

    async def transcribe_file(
        self, source: Path, options: BatchTranscriptionOptions
    ) -> TranscriptionResult:
        if not source.is_file():
            raise STTProviderError("Normalized media is unavailable.", code="STT_MEDIA_UNAVAILABLE", retryable=False)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        try:
            upload = await client.post(
                f"{self._api_base_url}/upload",
                headers={"authorization": self._key, "content-type": "application/octet-stream"},
                content=await asyncio.to_thread(source.read_bytes),
            )
            upload_url = self._response_json(upload).get("upload_url")
            if not isinstance(upload_url, str):
                raise STTProviderError("AssemblyAI did not return an upload URL.", code="STT_RESULT_INVALID", retryable=False)
            models = [self._model]
            if self._fallback_model and self._fallback_model not in models:
                models.append(self._fallback_model)
            request: dict[str, object] = {
                "audio_url": upload_url,
                "speech_models": models,
                "speaker_labels": options.diarization,
                "punctuate": True,
                "format_text": True,
                "disfluencies": False,
            }
            if options.language == "auto":
                # Universal-3.5 Pro natively handles code-switching. Steering it
                # toward the two languages exposed by the product is more
                # accurate than running a separate whole-file language guess.
                request["language_codes"] = ["vi", "en"]
            elif options.language in {"vi", "en"}:
                request["language_code"] = options.language
            prompt = _transcription_prompt(options.language, options.context)
            if prompt:
                request["prompt"] = prompt
            keyterms = _keyterms(options.keyterms)
            if keyterms:
                request["keyterms_prompt"] = keyterms
            submitted = await client.post(
                f"{self._api_base_url}/transcript",
                headers={"authorization": self._key},
                json=request,
            )
            transcript_id = self._response_json(submitted).get("id")
            if not isinstance(transcript_id, str):
                raise STTProviderError("AssemblyAI did not accept the transcript.", code="STT_RESULT_INVALID", retryable=False)
            deadline = time.monotonic() + self._poll_timeout
            while time.monotonic() < deadline:
                response = await client.get(
                    f"{self._api_base_url}/transcript/{transcript_id}",
                    headers={"authorization": self._key},
                )
                payload = self._response_json(response)
                state = payload.get("status")
                if state == "completed":
                    return _batch_result(payload)
                if state == "error":
                    raise STTProviderError(str(payload.get("error") or "AssemblyAI transcription failed."), code="STT_TRANSCRIPTION_FAILED", retryable=False)
                await asyncio.sleep(self._poll_interval)
            raise STTProviderError("AssemblyAI transcription timed out.", code="STT_TIMEOUT", retryable=True)
        finally:
            if owns_client:
                await client.aclose()

    async def open_stream(self, options: StreamingOptions) -> AssemblyAIStream:
        return await AssemblyAIStream.connect(
            self._key,
            self._streaming_base_url,
            self._streaming_model,
            self._streaming_mode,
            options,
            min_turn_silence_ms=self._streaming_min_turn_silence_ms,
            max_turn_silence_ms=self._streaming_max_turn_silence_ms,
        )

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            retryable = response.status_code >= 500 or response.status_code == 429
            raise STTProviderError("AssemblyAI request failed.", code="STT_PROVIDER_HTTP_ERROR", retryable=retryable)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise STTProviderError("AssemblyAI returned an invalid response.", code="STT_RESULT_INVALID", retryable=True) from exc
        if not isinstance(payload, dict):
            raise STTProviderError("AssemblyAI returned an invalid response.", code="STT_RESULT_INVALID", retryable=True)
        return payload


class AssemblyAIStream:
    def __init__(self, websocket: Any, language: str) -> None:
        self._websocket = websocket
        self._language = language
        self._events: deque[StreamingTranscriptionEvent] = deque()
        self._failure: STTProviderError | None = None
        self._terminated = asyncio.Event()
        self._closing = False
        self._closed = False
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(
        cls,
        api_key: str,
        streaming_base_url: str,
        streaming_model: str,
        streaming_mode: str,
        options: StreamingOptions,
        *,
        min_turn_silence_ms: int = 560,
        max_turn_silence_ms: int = 2_000,
    ) -> AssemblyAIStream:
        websocket: Any | None = None
        try:
            import websockets

            language = _streaming_language_code(options.language)
            language_codes = ["vi", "en"] if language == "auto" else [language]
            params: dict[str, object] = {
                "sample_rate": 16000,
                "encoding": options.audio_format or "pcm_s16le",
                "speech_model": streaming_model,
                "mode": streaming_mode,
                "format_turns": "true",
                # language_codes steers decoding; language_detection asks the
                # provider to report the detected language on each final turn.
                "language_codes": json.dumps(language_codes, ensure_ascii=False),
                "language_detection": "true",
                "min_turn_silence": min_turn_silence_ms,
                "max_turn_silence": max_turn_silence_ms,
            }
            if options.diarization:
                params["speaker_labels"] = "true"
            prompt = _transcription_prompt(language, options.context)
            if prompt:
                params["prompt"] = prompt
            keyterms = _keyterms(options.keyterms)
            if keyterms:
                # Streaming array parameters are JSON strings in the raw
                # WebSocket query (the SDK serializes these automatically).
                params["keyterms_prompt"] = json.dumps(keyterms, ensure_ascii=False)
            query = urlencode(params, doseq=True)
            websocket = await websockets.connect(
                f"{streaming_base_url}?{query}",
                additional_headers={"Authorization": api_key},
                ping_interval=20,
                ping_timeout=20,
            )
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise STTProviderError(
                    "AssemblyAI returned an invalid streaming handshake.",
                    code="STT_STREAM_HANDSHAKE_INVALID",
                    retryable=True,
                )
            if payload.get("type") == "Error":
                raise _stream_provider_error(payload, during_connect=True)
            if payload.get("type") != "Begin":
                raise STTProviderError(
                    "AssemblyAI did not start the streaming session.",
                    code="STT_STREAM_HANDSHAKE_INVALID",
                    retryable=True,
                )
            return cls(websocket, language)
        except STTProviderError:
            if websocket is not None:
                await websocket.close()
            raise
        except TimeoutError as exc:
            if websocket is not None:
                await websocket.close()
            raise STTProviderError(
                "AssemblyAI timed out while starting the streaming session.",
                code="STT_STREAM_CONNECT_TIMEOUT",
                retryable=True,
            ) from exc
        except Exception as exc:
            if websocket is not None:
                await websocket.close()
            raise STTProviderError(
                "Could not open the AssemblyAI streaming session.",
                code="STT_STREAM_CONNECT_FAILED",
                retryable=True,
            ) from exc

    async def _read(self) -> None:
        try:
            async for raw in self._websocket:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "Error":
                    self._failure = _stream_provider_error(payload)
                    return
                if event_type == "Termination":
                    if not self._closing:
                        self._failure = STTProviderError(
                            "AssemblyAI ended the streaming session unexpectedly.",
                            code="STT_STREAM_INTERRUPTED",
                            retryable=True,
                        )
                    return
                self._events.extend(_stream_events(payload, self._language))
        except asyncio.CancelledError:
            raise
        except json.JSONDecodeError:
            self._failure = STTProviderError(
                "AssemblyAI returned an invalid streaming event.",
                code="STT_STREAM_EVENT_INVALID",
                retryable=True,
            )
        except Exception as exc:
            if not self._closing:
                close_code = getattr(exc, "code", None)
                close_reason = str(getattr(exc, "reason", "") or "").strip()
                detail = f" (close code {close_code})" if close_code is not None else ""
                if close_reason:
                    detail += f": {close_reason}"
                self._failure = STTProviderError(
                    f"AssemblyAI closed the streaming session{detail}.",
                    code="STT_STREAM_INTERRUPTED",
                    retryable=close_code != 3006,
                )
        finally:
            self._terminated.set()

    async def send_audio(self, chunk: bytes) -> tuple[StreamingTranscriptionEvent, ...]:
        if self._closed or self._closing:
            raise STTProviderError(
                "The realtime session is closed.", code="STT_STREAM_CLOSED", retryable=False
            )
        self._raise_failure()
        if self._reader.done():
            raise STTProviderError(
                "AssemblyAI ended the streaming session unexpectedly.",
                code="STT_STREAM_INTERRUPTED",
                retryable=True,
            )
        try:
            await self._websocket.send(chunk)
            await asyncio.sleep(0)
        except Exception as exc:
            self._raise_failure()
            raise STTProviderError(
                "AssemblyAI lost the streaming session.",
                code="STT_STREAM_INTERRUPTED",
                retryable=True,
            ) from exc
        self._raise_failure()
        return self._drain()

    async def pause(self) -> tuple[StreamingTranscriptionEvent, ...]:
        self._raise_failure()
        return self._drain()

    async def resume(self) -> tuple[StreamingTranscriptionEvent, ...]:
        self._raise_failure()
        return self._drain()

    async def close(self) -> tuple[StreamingTranscriptionEvent, ...]:
        if self._closed:
            return self._drain()
        self._closing = True
        try:
            try:
                await self._websocket.send(json.dumps({"type": "Terminate"}))
            except Exception as exc:
                if self._failure is None:
                    self._failure = STTProviderError(
                        "AssemblyAI lost the streaming session while stopping.",
                        code="STT_STREAM_INTERRUPTED",
                        retryable=True,
                    )
                raise self._failure from exc
            try:
                await asyncio.wait_for(self._terminated.wait(), timeout=5)
            except TimeoutError as exc:
                self._failure = STTProviderError(
                    "AssemblyAI did not finish the streaming session in time.",
                    code="STT_STREAM_CLOSE_TIMEOUT",
                    retryable=True,
                )
                raise self._failure from exc
        finally:
            await self._websocket.close()
            if not self._reader.done():
                self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._closed = True
        self._raise_failure()
        return self._drain()

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _drain(self) -> tuple[StreamingTranscriptionEvent, ...]:
        events = tuple(event for event in self._events if event.segment.text.strip())
        self._events.clear()
        return events


def _stream_provider_error(
    payload: dict[str, Any], *, during_connect: bool = False
) -> STTProviderError:
    message = str(payload.get("error") or payload.get("message") or "AssemblyAI rejected the streaming session.")
    normalized = message.lower()
    retryable = not any(
        marker in normalized
        for marker in ("user input validation", "invalid", "unauthorized", "forbidden", "api key")
    )
    return STTProviderError(
        message,
        code="STT_STREAM_REJECTED" if during_connect else "STT_STREAM_PROVIDER_ERROR",
        retryable=retryable,
    )

def _transcription_prompt(language: str, context: str | None = None) -> str:
    # Universal Pro prompting expects a description of the recording, not
    # formatting or transcription instructions. A concise scenario prompt gives
    # names and domain vocabulary useful context without encouraging insertions.
    base = {
        "vi": "Vietnamese business meeting between multiple participants.",
        "en": "English business meeting between multiple participants.",
        "auto": "Bilingual Vietnamese and English business meeting between multiple participants.",
    }.get(language, "Business meeting between multiple participants.")
    detail = context.strip() if context else ""
    return f"{base} Topic and known context: {detail}".strip()[:1750] if detail else base


def _keyterms(values: tuple[str, ...]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        term = value.strip()
        if term and len(term) <= 50 and term.casefold() not in {item.casefold() for item in cleaned}:
            cleaned.append(term)
        if len(cleaned) == 100:
            break
    return cleaned


def _streaming_language_code(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-").split("-", 1)[0]
    if normalized not in {"vi", "en", "auto"}:
        raise STTProviderError(
            f"Realtime transcription does not support language '{language}'.",
            code="STT_LANGUAGE_UNSUPPORTED",
            retryable=False,
        )
    return normalized


def _batch_result(payload: dict[str, Any]) -> TranscriptionResult:
    utterances = payload.get("utterances")
    source = utterances if isinstance(utterances, list) and utterances else payload.get("words", [])
    segments: list[TranscriptionSegment] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("word")
        start, end = item.get("start"), item.get("end")
        if isinstance(text, str) and text.strip() and isinstance(start, int) and isinstance(end, int) and end > start:
            label = item.get("speaker")
            confidence = item.get("confidence")
            segments.append(TranscriptionSegment(start, end, text.strip(), str(label) if label is not None else None, float(confidence) if isinstance(confidence, (int, float)) else None))
    if not segments:
        if not str(payload.get("text") or "").strip() and not payload.get("words"):
            raise STTProviderError(
                "No speech was detected in the uploaded media.",
                code="STT_NO_SPEECH",
                retryable=False,
            )
        raise STTProviderError("AssemblyAI returned no timed transcript segments.", code="STT_RESULT_INVALID", retryable=False)
    language = str(payload.get("language_code") or "und").split("_")[0].lower()
    return TranscriptionResult(language, tuple(segments))


def _stream_events(
    payload: Any, default_language: str = "und"
) -> tuple[StreamingTranscriptionEvent, ...]:
    if not isinstance(payload, dict):
        return ()
    language = str(payload.get("language_code") or default_language)
    if payload.get("type") == "SpeakerRevision":
        events: list[StreamingTranscriptionEvent] = []
        revisions = payload.get("revisions")
        if not isinstance(revisions, list):
            return ()
        for revision in revisions:
            if not isinstance(revision, dict):
                continue
            words = revision.get("words") if isinstance(revision.get("words"), list) else []
            timing = _word_timing(words)
            text = _words_text(words)
            turn_order = revision.get("turn_order")
            if timing is None or not text or turn_order is None:
                continue
            events.append(
                StreamingTranscriptionEvent(
                    "speaker_revision",
                    language,
                    TranscriptionSegment(
                        timing[0],
                        timing[1],
                        text,
                        speaker_label=_speaker_label(revision),
                        confidence=_word_confidence(words),
                    ),
                    provider_segment_id=str(turn_order),
                )
            )
        return tuple(events)
    if payload.get("type") != "Turn" or not str(payload.get("transcript", "")).strip():
        return ()
    words = payload.get("words") if isinstance(payload.get("words"), list) else []
    timing = _word_timing(words)
    if timing is None:
        return ()
    turn_order = payload.get("turn_order")
    return (
        StreamingTranscriptionEvent(
            "final" if payload.get("end_of_turn") else "partial",
            language,
            TranscriptionSegment(
                timing[0],
                timing[1],
                str(payload["transcript"]).strip(),
                speaker_label=_speaker_label(payload),
                confidence=_word_confidence(words),
            ),
            provider_segment_id=str(turn_order) if turn_order is not None else None,
        ),
    )


def _stream_event(
    payload: Any, default_language: str = "und"
) -> StreamingTranscriptionEvent | None:
    """Compatibility helper for callers that expect a single normalized event."""
    events = _stream_events(payload, default_language)
    return events[0] if events else None


def _word_timing(words: list[Any]) -> tuple[int, int] | None:
    valid = [item for item in words if isinstance(item, dict)]
    if not valid:
        return None
    start, end = valid[0].get("start"), valid[-1].get("end")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    return start, end


def _word_confidence(words: list[Any]) -> float | None:
    values = [
        float(item["confidence"])
        for item in words
        if isinstance(item, dict) and isinstance(item.get("confidence"), (int, float))
    ]
    return sum(values) / len(values) if values else None


def _speaker_label(payload: dict[str, Any]) -> str | None:
    value = payload.get("speaker_label")
    if value in {None, "", "PENDING", "UNKNOWN"}:
        return None
    return str(value)


def _words_text(words: list[Any]) -> str:
    return " ".join(
        str(item.get("text") or item.get("word") or "").strip()
        for item in words
        if isinstance(item, dict) and str(item.get("text") or item.get("word") or "").strip()
    ).strip()


def build_assemblyai_provider(settings: Settings) -> AssemblyAIProvider:
    return AssemblyAIProvider(settings)
