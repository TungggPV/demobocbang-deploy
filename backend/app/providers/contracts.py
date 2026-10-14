"""Normalized contracts that keep vendor SDKs out of domain services."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


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
class TranscriptionSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    segments: tuple[TranscriptionSegment, ...]


@dataclass(frozen=True)
class BatchTranscriptionOptions:
    """Provider-neutral context for one pre-recorded transcription job."""

    language: str
    diarization: bool
    expected_speakers: int | None = None
    context: str | None = None
    keyterms: tuple[str, ...] = ()


@dataclass(frozen=True)
class StreamingOptions:
    """Provider-neutral context supplied when a realtime STT session opens."""

    language: str
    diarization: bool
    expected_speakers: int | None = None
    audio_format: str | None = None
    context: str | None = None
    keyterms: tuple[str, ...] = ()


@dataclass(frozen=True)
class StreamingTranscriptionEvent:
    """One normalized incremental result from a streaming STT provider."""

    type: Literal["partial", "final", "speaker_revision"]
    language: str
    segment: TranscriptionSegment
    provider_segment_id: str | None = None


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

    async def send_audio(self, chunk: bytes) -> tuple[StreamingTranscriptionEvent, ...]: ...

    async def pause(self) -> tuple[StreamingTranscriptionEvent, ...]: ...

    async def resume(self) -> tuple[StreamingTranscriptionEvent, ...]: ...

    async def close(self) -> tuple[StreamingTranscriptionEvent, ...]: ...


class STTProvider(Protocol):
    def capabilities(self) -> STTCapabilities: ...

    async def transcribe_file(
        self, source: Path, options: BatchTranscriptionOptions
    ) -> TranscriptionResult: ...

    async def open_stream(self, options: StreamingOptions) -> STTStream: ...


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

    async def generate_summary(
        self, transcript: str, template: str, output_language: str
    ) -> dict[str, object]: ...


class LLMProviderError(Exception):
    def __init__(self, message: str, *, code: str = "LLM_PROVIDER_ERROR", retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)
