"""Public capability discovery for provider-dependent client controls."""

from typing import cast

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.providers.contracts import LLMProvider, STTProvider, TranslationProvider

router = APIRouter()


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, object]:
    settings = cast(Settings, request.app.state.settings)
    provider = cast(STTProvider | None, getattr(request.app.state, "stt_provider", None))
    translation_provider = cast(TranslationProvider | None, getattr(request.app.state, "translation_provider", None))
    llm_provider = cast(LLMProvider | None, getattr(request.app.state, "llm_provider", None))
    if provider is None:
        stt: dict[str, object] = {
            "provider": settings.stt_provider or "unconfigured",
            "fallback_provider": settings.stt_fallback_provider,
            "batch": False,
            "streaming": False,
            "speaker_identification": False,
            "diarization": False,
            "timestamps": False,
            "confidence": False,
            "languages": [],
        }
    else:
        available = provider.capabilities()
        stt = {
            "provider": settings.stt_provider or provider.__class__.__name__,
            "fallback_provider": settings.stt_fallback_provider,
            "batch": available.batch,
            "streaming": available.streaming,
            "speaker_identification": bool(
                settings.speechmatics_api_key and available.diarization
            ),
            "diarization": available.diarization,
            "timestamps": available.timestamps,
            "confidence": available.confidence,
            "languages": sorted(available.supported_languages),
        }
    pairs = sorted(translation_provider.capabilities().supported_language_pairs) if translation_provider else []
    return {
        "stt": stt,
        "meeting_voice_learning": bool(getattr(request.app.state, "speaker_enrollment_provider", None)),
        "transcript_polish": bool(llm_provider and callable(getattr(llm_provider, "polish_transcript", None))),
        "translation": {"languages": sorted({language for pair in pairs for language in pair}), "language_pairs": [list(pair) for pair in pairs]},
        "summary_templates": ["general", "briefing", "voting"] if llm_provider and llm_provider.capabilities().structured_output else [],
        "custom_summary_prompt": bool(llm_provider and llm_provider.capabilities().structured_output),
        "summary_languages": ["vi", "en"] if llm_provider and llm_provider.capabilities().structured_output else [],
        "exports": ["pdf", "docx", "txt", "srt", "vtt", "json"],
    }
