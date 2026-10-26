"""Normalized contracts that keep vendor SDKs out of domain services."""

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True)
class STTCapabilities:
    batch: bool
    streaming: bool
    diarization: bool
    timestamps: bool
    confidence: bool
    supported_languages: frozenset[str]
    multilingual_auto_detect: bool


@dataclass(frozen=True)
class KnownSpeaker:
    profile_id: UUID
    provider_label: str
    name: str
    speaker_identifiers: tuple[str, ...]


@dataclass(frozen=True)
class SpeakerEnrollment:
    speaker_identifiers: tuple[str, ...]
    duration_ms: int


@dataclass(frozen=True)
class TranscriptionSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None = None
    confidence: float | None = None
    speaker_name: str | None = None
    voice_profile_id: UUID | None = None
    is_identified: bool = False


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    segments: tuple[TranscriptionSegment, ...]
    provider_name: str | None = None
    fallback_active: bool = False


@dataclass(frozen=True)
class StreamingOptions:
    """Provider-neutral context supplied when a realtime STT session opens."""

    language: str
    diarization: bool
    audio_format: str | None = None
    known_speakers: tuple[KnownSpeaker, ...] = ()


@dataclass(frozen=True)
class StreamingTranscriptionEvent:
    """One normalized incremental result from a streaming STT provider."""

    type: Literal["partial", "final"]
    language: str
    segment: TranscriptionSegment
    turn_order: int | None = None
    stream_segment_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid4().hex)
    captured_at: float = field(default_factory=perf_counter)


@dataclass(frozen=True)
class StreamingSpeakerRevision:
    """A corrected label for a previously finalized turn in this stream."""

    turn_order: int
    speaker_label: str | None


StreamingEvent = StreamingTranscriptionEvent | StreamingSpeakerRevision


class STTProviderError(Exception):
    """A provider failure safe to expose through the public realtime protocol."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "STT_PROVIDER_ERROR",
        retryable: bool = True,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class STTStream(Protocol):
    """A live provider connection. Implementations must not retain API keys in events."""

    provider_name: str
    speaker_identification_available: bool
    fallback_active: bool

    async def send_audio(self, chunk: bytes) -> tuple[StreamingEvent, ...]: ...

    async def receive_events(self) -> tuple[StreamingEvent, ...]:
        """Wait for provider output independently of incoming audio; cancellation safe."""
        ...

    async def pause(self) -> tuple[StreamingEvent, ...]: ...

    async def resume(self) -> tuple[StreamingEvent, ...]: ...

    async def close(self) -> tuple[StreamingEvent, ...]: ...


class STTProvider(Protocol):
    def capabilities(self) -> STTCapabilities: ...

    async def transcribe_file(
        self,
        source: Path,
        language: str | None = None,
        known_speakers: tuple[KnownSpeaker, ...] = (),
    ) -> TranscriptionResult: ...

    async def open_stream(self, options: StreamingOptions) -> STTStream: ...


class SpeakerEnrollmentProvider(Protocol):
    async def enroll_speaker(
        self, source: Path, language: str = "vi"
    ) -> SpeakerEnrollment: ...


@dataclass(frozen=True)
class TranslationCapabilities:
    supported_language_pairs: frozenset[tuple[str, str]]


class TranslationProvider(Protocol):
    def capabilities(self) -> TranslationCapabilities: ...

    async def translate_segments(
        self, segments: tuple[TranscriptionSegment, ...], source: str, target: str
    ) -> tuple[TranscriptionSegment, ...]: ...


class TranslationProviderError(Exception):
    def __init__(self, message: str, *, code: str = "TRANSLATION_PROVIDER_ERROR", retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class LLMCapabilities:
    structured_output: bool
    max_context_tokens: int | None


class LLMProvider(Protocol):
    def capabilities(self) -> LLMCapabilities: ...

    async def polish_transcript(self, segments: tuple[str, ...], context: str) -> tuple[str, ...]: ...

    async def generate_summary(
        self,
        transcript: str,
        prompt: str | None,
        output_language: str,
        template: str | None = None,
    ) -> dict[str, object]: ...


class LLMProviderError(Exception):
    def __init__(self, message: str, *, code: str = "LLM_PROVIDER_ERROR", retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)
