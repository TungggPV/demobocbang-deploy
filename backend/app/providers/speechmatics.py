"""Speechmatics batch, realtime, and speaker-enrollment adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.providers.contracts import (
    KnownSpeaker,
    SpeakerEnrollment,
    StreamingEvent,
    StreamingOptions,
    StreamingTranscriptionEvent,
    STTCapabilities,
    STTProviderError,
    TranscriptionResult,
    TranscriptionSegment,
)

logger = logging.getLogger(__name__)
_TIMESTAMP_TOLERANCE_MS = 150


class SpeechmaticsProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        batch_client_factory: Callable[..., Any] | None = None,
        voice_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not settings.speechmatics_api_key:
            raise ValueError(
                "SPEECHMATICS_API_KEY is required when STT_PROVIDER=speechmatics."
            )
        self._key = settings.speechmatics_api_key
        self._batch_url = settings.speechmatics_batch_url
        self._rt_url = settings.speechmatics_rt_url
        self._sensitivity = settings.speechmatics_speaker_sensitivity
        self._prefer_current = settings.speechmatics_prefer_current_speaker
        self._silence_trigger = settings.speechmatics_end_of_utterance_silence_seconds
        self._max_delay = settings.speechmatics_max_delay_seconds
        self._finalization_delay = settings.speechmatics_finalization_delay_seconds
        if self._silence_trigger >= self._max_delay:
            raise ValueError("Speechmatics silence trigger must be lower than max delay.")
        self._batch_client_factory = batch_client_factory
        self._voice_client_factory = voice_client_factory

    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(
            batch=True,
            streaming=True,
            diarization=True,
            timestamps=True,
            confidence=True,
            supported_languages=frozenset({"vi", "en"}),
            multilingual_auto_detect=False,
        )

    async def enroll_speaker(
        self, source: Path, language: str = "vi"
    ) -> SpeakerEnrollment:
        if not source.is_file():
            raise STTProviderError(
                "Normalized media is unavailable.",
                code="STT_MEDIA_UNAVAILABLE",
                retryable=False,
            )
        try:
            transcript = await self._run_batch(
                source,
                language,
                speaker_config={
                    "get_speakers": True,
                    "prefer_current_speaker": self._prefer_current,
                    "speaker_sensitivity": self._sensitivity,
                },
            )
        except STTProviderError:
            raise
        except Exception as exc:
            raise _provider_error(exc, operation="speaker enrollment") from exc

        speakers = list(getattr(transcript, "speakers", None) or [])
        if not speakers:
            raise STTProviderError(
                "Speechmatics did not find a usable voice in the sample.",
                code="VOICE_PROFILE_NO_SPEECH",
                retryable=False,
            )
        if len(speakers) != 1:
            raise STTProviderError(
                "The voice sample must contain exactly one speaker.",
                code="VOICE_PROFILE_MULTIPLE_SPEAKERS",
                retryable=False,
            )
        identifiers = tuple(
            dict.fromkeys(str(value) for value in speakers[0].speaker_identifiers if value)
        )
        if not identifiers:
            raise STTProviderError(
                "Speechmatics did not return a speaker identifier.",
                code="VOICE_PROFILE_NO_IDENTIFIER",
                retryable=False,
            )
        duration = max(
            (
                float(getattr(item, "end_time", 0.0) or 0.0)
                for item in (getattr(transcript, "results", None) or [])
            ),
            default=0.0,
        )
        return SpeakerEnrollment(identifiers, max(1, round(duration * 1000)))

    async def transcribe_file(
        self,
        source: Path,
        language: str | None = None,
        known_speakers: tuple[KnownSpeaker, ...] = (),
    ) -> TranscriptionResult:
        if not source.is_file():
            raise STTProviderError(
                "Normalized media is unavailable.",
                code="STT_MEDIA_UNAVAILABLE",
                retryable=False,
            )
        normalized_language = _language(language)
        speaker_config: dict[str, Any] = {
            "prefer_current_speaker": self._prefer_current,
            "speaker_sensitivity": self._sensitivity,
        }
        if known_speakers:
            speaker_config["speakers"] = [
                {
                    "label": speaker.provider_label,
                    "speaker_identifiers": list(speaker.speaker_identifiers),
                }
                for speaker in known_speakers
            ]
        try:
            transcript = await self._run_batch(
                source,
                normalized_language,
                speaker_config=speaker_config,
            )
            return _batch_result(transcript, normalized_language, known_speakers)
        except STTProviderError:
            raise
        except Exception as exc:
            raise _provider_error(exc, operation="batch transcription") from exc

    async def _run_batch(
        self, source: Path, language: str, *, speaker_config: dict[str, Any]
    ) -> Any:
        from speechmatics.batch import AsyncClient, FormatType, TranscriptionConfig

        factory = self._batch_client_factory or AsyncClient
        kwargs: dict[str, Any] = {"api_key": self._key}
        if self._batch_url:
            kwargs["url"] = self._batch_url
        async with factory(**kwargs) as client:
            job = await client.submit_job(
                str(source),
                transcription_config=TranscriptionConfig(
                    language=_language(language),
                    diarization="speaker",
                    speaker_diarization_config=speaker_config,
                ),
            )
            return await client.wait_for_completion(job.id, format_type=FormatType.JSON)

    async def open_stream(self, options: StreamingOptions) -> SpeechmaticsStream:
        try:
            from speechmatics.voice import (
                EndOfUtteranceMode,
                SpeakerIdentifier,
                SpeechSegmentConfig,
                VoiceAgentClient,
                VoiceAgentConfig,
                VoiceAgentConfigPreset,
            )

            config = VoiceAgentConfigPreset.ADAPTIVE(
                VoiceAgentConfig(
                    language=_language(options.language),
                    enable_diarization=options.diarization,
                    speaker_sensitivity=self._sensitivity,
                    prefer_current_speaker=self._prefer_current,
                    known_speakers=[
                        SpeakerIdentifier(
                            label=speaker.provider_label,
                            speaker_identifiers=list(speaker.speaker_identifiers),
                        )
                        for speaker in options.known_speakers
                    ],
                    end_of_utterance_mode=EndOfUtteranceMode.ADAPTIVE,
                    # The SDK's bare custom-config defaults split normal pauses
                    # too aggressively. Overlay the official ADAPTIVE preset and
                    # use a one-second base silence window for meeting speech.
                    end_of_utterance_silence_trigger=self._silence_trigger,
                    # Final fragments are debounced into durable turns below.
                    speech_segment_config=SpeechSegmentConfig(emit_sentences=False),
                    include_partials=True,
                    max_delay=self._max_delay,
                    include_results=True,
                    sample_rate=16000,
                )
            )
            factory = self._voice_client_factory or VoiceAgentClient
            client = factory(
                api_key=self._key,
                url=self._rt_url,
                config=config,
            )
            stream = SpeechmaticsStream(
                client,
                options,
                finalization_delay_seconds=self._finalization_delay,
                max_segment_gap_seconds=self._silence_trigger,
            )
            await stream.connect()
            return stream
        except STTProviderError:
            raise
        except Exception as exc:
            raise _provider_error(exc, operation="realtime connection") from exc


@dataclass(eq=False)
class _StreamingTurn:
    """One display row, independent of mutable provider fragment timestamps."""

    id: int
    language: str
    speaker_label: str | None
    finals: list[TranscriptionSegment] = field(default_factory=list)
    partials: list[TranscriptionSegment] = field(default_factory=list)
    captured_at: float | None = None
    last_partial_at: float = 0
    last_final_at: float = 0
    flush_handle: asyncio.TimerHandle | None = None

    @property
    def start_ms(self) -> int:
        return min(segment.start_ms for segment in self.finals + self.partials)

    @property
    def end_ms(self) -> int:
        return max(segment.end_ms for segment in self.finals + self.partials)

    @property
    def awaiting_final(self) -> bool:
        # Never publish unconfirmed words or close a row while its tail is interim.
        return bool(self.partials)


class SpeechmaticsStream:
    provider_name = "speechmatics"
    fallback_active = False

    def __init__(
        self,
        client: Any,
        options: StreamingOptions,
        *,
        finalization_delay_seconds: float = 3.0,
        max_segment_gap_seconds: float = 1.0,
    ) -> None:
        self._client = client
        self._language = _language(options.language)
        self._diarization = options.diarization
        self._known = {item.provider_label: item for item in options.known_speakers}
        self.speaker_identification_available = bool(options.diarization and self._known)
        self._events: deque[StreamingEvent] = deque()
        self._events_ready = asyncio.Event()
        self._failure: STTProviderError | None = None
        self._turns: list[_StreamingTurn] = []
        self._active_turn: _StreamingTurn | None = None
        self._next_turn_id = 0
        self._finalization_delay_seconds = finalization_delay_seconds
        self._max_segment_gap_ms = round(max_segment_gap_seconds * 1000)
        self._max_turn_duration_ms = 30_000
        self._finalized_end_ms: dict[tuple[str, str | None], int] = {}
        self._final_keys: set[tuple[int, int, str | None, str]] = set()
        self._completed_fragments: set[tuple[int, int, str | None, str]] = set()
        self._turn_order = 0
        self._closed = False

    async def connect(self) -> None:
        from speechmatics.voice import AgentServerMessageType

        self._client.on(
            AgentServerMessageType.ADD_PARTIAL_SEGMENT,
            lambda payload: self._capture(payload, final=False),
        )
        self._client.on(
            AgentServerMessageType.ADD_SEGMENT,
            lambda payload: self._capture(payload, final=True),
        )
        self._client.on(
            AgentServerMessageType.END_OF_TURN,
            self._capture_end_of_turn,
        )
        self._client.on(AgentServerMessageType.ERROR, self._capture_error)
        try:
            await self._client.connect()
        except Exception as exc:
            raise _provider_error(exc, operation="realtime connection") from exc

    def _capture(self, payload: Any, *, final: bool) -> None:
        if not isinstance(payload, dict):
            return
        captured: list[tuple[str, TranscriptionSegment, bool]] = []
        for item in payload.get("segments", []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            start_ms = _milliseconds(metadata.get("start_time"))
            end_ms = _milliseconds(metadata.get("end_time"))
            if not text or end_ms <= start_ms:
                continue
            label = str(item.get("speaker_id")) if item.get("speaker_id") else None
            if not self._diarization:
                label = None
            known = self._known.get(label or "")
            captured.append(
                (
                    str(item.get("language") or self._language),
                    TranscriptionSegment(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        text=text,
                        speaker_label=label,
                        speaker_name=known.name if known else None,
                        voice_profile_id=known.profile_id if known else None,
                        is_identified=known is not None,
                    ),
                    item.get("is_eou") is True,
                )
            )

        changed: list[_StreamingTurn] = []
        for language, segment, is_eou in sorted(captured, key=lambda item: item[1].start_ms):
            fragment_key = (
                segment.start_ms, segment.end_ms, segment.speaker_label, segment.text
            )
            if fragment_key in self._completed_fragments:
                continue
            turn = self._find_turn(language, segment)
            if turn is None:
                continue
            if final:
                turn.last_final_at = perf_counter()
                if turn.captured_at is None:
                    turn.captured_at = turn.last_final_at
                turn.finals = self._replace_overlapping(turn.finals, segment)
                turn.partials = [
                    partial for partial in turn.partials
                    if not (
                        self._covered_by_finals(turn, partial)
                        or (is_eou and self._within_final_boundary(
                            partial, segment.start_ms, segment.end_ms,
                        ))
                    )
                ]
            elif self._covered_by_finals(turn, segment):
                continue
            else:
                turn.last_partial_at = perf_counter()
                turn.partials = self._replace_overlapping(turn.partials, segment)
            self._schedule_final_flush(turn)
            if turn not in changed:
                changed.append(turn)
        for turn in changed:
            if turn in self._turns:
                self._preview(turn)

    def _find_turn(self, language: str, segment: TranscriptionSegment) -> _StreamingTurn | None:
        identity = (language, segment.speaker_label)
        # Late finals can still resolve an older speaker's unfinished preview.
        for turn in reversed(self._turns):
            if (turn.language, turn.speaker_label) != identity:
                continue
            overlap = segment.start_ms < turn.end_ms and segment.end_ms > turn.start_ms
            adjacent = (
                turn is self._active_turn
                and 0 <= segment.start_ms - turn.end_ms <= self._max_segment_gap_ms
                and segment.end_ms - turn.start_ms <= self._max_turn_duration_ms
            )
            if overlap or adjacent:
                return turn
        if segment.end_ms <= self._finalized_end_ms.get(identity, -1):
            return None
        if self._active_turn is not None:
            # Genuine speaker/gap boundaries close stable text immediately, but
            # unresolved previews remain available for their delayed final event.
            self._flush_turn(self._active_turn)
        turn = _StreamingTurn(self._next_turn_id, language, segment.speaker_label)
        self._next_turn_id += 1
        self._turns.append(turn)
        self._active_turn = turn
        return turn

    @staticmethod
    def _covered_by_finals(turn: _StreamingTurn, segment: TranscriptionSegment) -> bool:
        if not turn.finals:
            return False
        stable_start = min(item.start_ms for item in turn.finals)
        stable_end = max(item.end_ms for item in turn.finals)
        if segment.start_ms >= stable_start - _TIMESTAMP_TOLERANCE_MS:
            if segment.end_ms <= stable_end:
                return True
        # An unchanged phrase can have different final timing. Confirmed text
        # resolves that snapshot, but must not swallow additional interim words.
        overlapping = sorted(
            (item for item in turn.finals if (
                item.start_ms < segment.end_ms and item.end_ms > segment.start_ms
            )),
            key=lambda item: item.start_ms,
        )
        return bool(overlapping) and (
            re.findall(r"\w+", segment.text.casefold())
            == re.findall(r"\w+", " ".join(item.text for item in overlapping).casefold())
        )

    @staticmethod
    def _within_final_boundary(segment: TranscriptionSegment, start: int, end: int) -> bool:
        return (
            segment.start_ms < end
            and segment.end_ms > start
        )

    def _capture_end_of_turn(self, payload: Any) -> None:
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            try:
                start = _milliseconds(metadata.get("start_time"))
                end = _milliseconds(metadata.get("end_time"))
            except (TypeError, ValueError):
                start = end = 0
            if end > start:
                for turn in self._turns:
                    if turn.last_partial_at > turn.last_final_at:
                        continue
                    if any(
                        item.start_ms < end and item.end_ms > start for item in turn.finals
                    ):
                        turn.partials = [
                            item for item in turn.partials
                            if not self._within_final_boundary(item, start, end)
                        ]
        # Empty/old boundaries cannot promote an unconfirmed newer tail.
        self._schedule_final_flush()

    @staticmethod
    def _replace_overlapping(
        fragments: list[TranscriptionSegment], segment: TranscriptionSegment,
    ) -> list[TranscriptionSegment]:
        # Overlapping partials are revised snapshots, not extra words to append.
        return sorted(
            [previous for previous in fragments if not (
                segment.start_ms < previous.end_ms and segment.end_ms > previous.start_ms
            )] + [segment],
            key=lambda item: item.start_ms,
        )

    def _combined(
        self, turn: _StreamingTurn, *, preview: bool,
    ) -> TranscriptionSegment:
        fragments = list(turn.finals)
        if preview:
            for partial in turn.partials:
                fragments = self._replace_overlapping(fragments, partial)
        # Boundaries are checked on ingestion. A long cumulative SDK snapshot
        # can span more than the adjacent-fragment limit; never drop its tail.
        return _coalesce_stream_segments(
            [(turn.language, segment) for segment in sorted(
                fragments, key=lambda item: item.start_ms,
            )],
            max_gap_ms=turn.end_ms - turn.start_ms,
        )[0][1]

    def _preview(self, turn: _StreamingTurn) -> None:
        event = StreamingTranscriptionEvent(
            "partial", turn.language, self._combined(turn, preview=True),
            stream_segment_id=f"turn:{turn.id}",
        )
        self._events.append(event)
        logger.debug(
            "STT provider event=%s type=partial segment=%s audio_end_ms=%s",
            event.event_id, event.stream_segment_id, event.segment.end_ms,
        )
        self._events_ready.set()

    def _flush_turn(self, turn: _StreamingTurn, *, force: bool = False) -> None:
        if turn.flush_handle is not None:
            turn.flush_handle.cancel()
            turn.flush_handle = None
        if turn not in self._turns or (turn.awaiting_final and not force):
            return
        if turn.finals:
            segment = self._combined(turn, preview=False)
            key = (
                segment.start_ms,
                segment.end_ms,
                segment.speaker_label,
                segment.text,
            )
            if key not in self._final_keys:
                self._final_keys.add(key)
                self._events.append(
                    StreamingTranscriptionEvent(
                        "final", turn.language, segment,
                        turn_order=self._turn_order,
                        stream_segment_id=f"turn:{turn.id}",
                        captured_at=turn.captured_at or perf_counter(),
                    )
                )
                self._turn_order += 1
                self._events_ready.set()
            self._completed_fragments.update(
                (item.start_ms, item.end_ms, item.speaker_label, item.text)
                for item in turn.finals
            )
            identity = (turn.language, turn.speaker_label)
            self._finalized_end_ms[identity] = max(
                segment.end_ms, self._finalized_end_ms.get(identity, -1),
            )
        if turn in self._turns:
            self._turns.remove(turn)
        if self._active_turn is turn:
            self._active_turn = None

    def _flush_pending_finals(self) -> None:
        for turn in list(self._turns):
            self._flush_turn(turn, force=True)

    def _schedule_final_flush(self, turn: _StreamingTurn | None = None) -> None:
        for pending in ([turn] if turn is not None else list(self._turns)):
            if pending.flush_handle is not None:
                pending.flush_handle.cancel()
                pending.flush_handle = None
            if pending.finals and not pending.awaiting_final:
                pending.flush_handle = asyncio.get_running_loop().call_later(
                    self._finalization_delay_seconds, self._flush_turn, pending,
                )

    def _capture_error(self, payload: Any) -> None:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        self._failure = STTProviderError(
            str(reason or "Speechmatics realtime transcription failed."),
            code="STT_STREAM_PROVIDER_ERROR",
            retryable=True,
        )
        self._events_ready.set()

    async def receive_events(self) -> tuple[StreamingEvent, ...]:
        await self._events_ready.wait()
        self._raise_failure()
        return self._drain()

    async def send_audio(self, chunk: bytes) -> tuple[StreamingEvent, ...]:
        if self._closed:
            raise STTProviderError(
                "The realtime session is closed.",
                code="STT_STREAM_CLOSED",
                retryable=False,
            )
        self._raise_failure()
        try:
            await self._client.send_audio(chunk)
            await asyncio.sleep(0)
        except Exception as exc:
            self._raise_failure()
            raise _provider_error(exc, operation="realtime audio") from exc
        self._raise_failure()
        return self._drain()

    async def pause(self) -> tuple[StreamingEvent, ...]:
        self._raise_failure()
        return self._drain()

    async def resume(self) -> tuple[StreamingEvent, ...]:
        self._raise_failure()
        return self._drain()

    async def close(self) -> tuple[StreamingEvent, ...]:
        if self._closed:
            return self._drain()
        try:
            await self._client.disconnect()
            await asyncio.sleep(0)
            # The SDK normally emits END_OF_TURN while disconnecting. Flush as
            # a safeguard for transports that only deliver the last fragments.
            self._flush_pending_finals()
        except Exception as exc:
            raise _provider_error(exc, operation="realtime close") from exc
        finally:
            self._closed = True
            for turn in self._turns:
                if turn.flush_handle is not None:
                    turn.flush_handle.cancel()
                    turn.flush_handle = None
        self._raise_failure()
        return self._drain()

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _drain(self) -> tuple[StreamingEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        self._events_ready.clear()
        return events


def _batch_result(
    transcript: Any,
    default_language: str,
    known_speakers: tuple[KnownSpeaker, ...],
) -> TranscriptionResult:
    known = {item.provider_label: item for item in known_speakers}
    grouped: list[dict[str, Any]] = []
    for result in getattr(transcript, "results", None) or []:
        alternatives = getattr(result, "alternatives", None) or []
        if not alternatives or getattr(result, "type", None) not in {"word", "punctuation"}:
            continue
        alternative = alternatives[0]
        content = str(getattr(alternative, "content", "") or "")
        if not content:
            continue
        speaker = getattr(alternative, "speaker", None)
        label = None if speaker in {None, "", "UU"} else str(speaker)
        start_ms = _milliseconds(getattr(result, "start_time", 0.0))
        end_ms = _milliseconds(getattr(result, "end_time", 0.0))
        if end_ms <= start_ms:
            continue
        confidence = getattr(alternative, "confidence", None)
        language = str(getattr(alternative, "language", None) or default_language)
        if not grouped or grouped[-1]["speaker_label"] != label:
            grouped.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker_label": label,
                    "parts": [],
                    "confidences": [],
                    "language": language,
                }
            )
        group = grouped[-1]
        group["end_ms"] = max(group["end_ms"], end_ms)
        group["parts"].append((content, getattr(result, "type", None)))
        if isinstance(confidence, (int, float)):
            group["confidences"].append(float(confidence))

    segments: list[TranscriptionSegment] = []
    for group in grouped:
        text = _join_parts(group["parts"])
        if not text:
            continue
        identified = known.get(group["speaker_label"] or "")
        confidence_values = group["confidences"]
        segments.append(
            TranscriptionSegment(
                start_ms=group["start_ms"],
                end_ms=group["end_ms"],
                text=text,
                speaker_label=group["speaker_label"],
                confidence=(
                    sum(confidence_values) / len(confidence_values)
                    if confidence_values
                    else None
                ),
                speaker_name=identified.name if identified else None,
                voice_profile_id=identified.profile_id if identified else None,
                is_identified=identified is not None,
            )
        )
    if not segments:
        raise STTProviderError(
            "Speechmatics returned no timed transcript segments.",
            code="STT_RESULT_INVALID",
            retryable=False,
        )
    return TranscriptionResult(
        default_language, tuple(segments), provider_name="speechmatics"
    )


def _join_parts(parts: list[tuple[str, str]]) -> str:
    result = ""
    for content, item_type in parts:
        if not result:
            result = content
        elif item_type == "punctuation" or content.strip() in ".,!?;:)]}" :
            result += content
        else:
            result += " " + content
    return result.strip()


def _coalesce_stream_segments(
    captured: list[tuple[str, TranscriptionSegment]],
    *, max_gap_ms: int = 1500,
) -> list[tuple[str, TranscriptionSegment]]:
    grouped: list[tuple[str, TranscriptionSegment]] = []
    for language, segment in captured:
        if not grouped:
            grouped.append((language, segment))
            continue
        previous_language, previous = grouped[-1]
        gap_ms = segment.start_ms - previous.end_ms
        if (
            language != previous_language
            or segment.speaker_label != previous.speaker_label
            or gap_ms > max_gap_ms
        ):
            grouped.append((language, segment))
            continue
        separator = ""
        if not segment.text.startswith((".", ",", "!", "?", ";", ":", ")", "]", "}")):
            separator = " "
        grouped[-1] = (
            language,
            TranscriptionSegment(
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, segment.end_ms),
                text=f"{previous.text.rstrip()}{separator}{segment.text.lstrip()}",
                speaker_label=previous.speaker_label,
                confidence=previous.confidence,
                speaker_name=previous.speaker_name,
                voice_profile_id=previous.voice_profile_id,
                is_identified=previous.is_identified,
            ),
        )
    return grouped


def _milliseconds(value: Any) -> int:
    return max(0, round(float(value or 0.0) * 1000))


def _language(value: str | None) -> str:
    normalized = str(value or "vi").strip().lower().replace("_", "-").split("-", 1)[0]
    if normalized not in {"vi", "en"}:
        raise STTProviderError(
            f"Speechmatics does not support configured language '{value}'.",
            code="STT_LANGUAGE_UNSUPPORTED",
            retryable=False,
        )
    return normalized


def _provider_error(exc: Exception, *, operation: str) -> STTProviderError:
    message = str(exc).strip() or exc.__class__.__name__
    normalized = message.lower()
    nonretryable = any(
        marker in normalized
        for marker in (
            "api key",
            "unauthorized",
            "forbidden",
            "401",
            "403",
            "invalid config",
            "validation error",
        )
    )
    return STTProviderError(
        f"Speechmatics {operation} failed: {message}",
        code="STT_PROVIDER_CONFIGURATION_ERROR" if nonretryable else "STT_PROVIDER_ERROR",
        retryable=not nonretryable,
    )


def build_speechmatics_provider(settings: Settings) -> SpeechmaticsProvider:
    return SpeechmaticsProvider(settings)
