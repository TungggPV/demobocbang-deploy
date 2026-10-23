"""Gemini translation and structured-summary adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.providers.contracts import (
    LLMCapabilities,
    LLMProviderError,
    TranscriptionSegment,
    TranslationCapabilities,
    TranslationProviderError,
)

_SUMMARY_KEYS = ("overview", "key_points", "action_items", "decisions", "risks_notes")


class _GeminiBase:
    def __init__(self, api_key: str | None, model: str, api_base_url: str) -> None:
        if not api_key:
            raise ValueError("A Gemini API key is required for the configured provider.")
        self._key, self._model = api_key, model
        self._api_base_url = api_base_url.rstrip("/")

    async def _generate(self, prompt: str, schema: dict[str, object]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                response = await client.post(
                    f"{self._api_base_url}/models/{self._model}:generateContent",
                    headers={"x-goog-api-key": self._key},
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema}},
                )
        except httpx.HTTPError as exc:
            raise LLMProviderError("Gemini could not be reached.", code="GEMINI_NETWORK_ERROR", retryable=True) from exc
        if response.is_error:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise LLMProviderError("Gemini rejected the request.", code="GEMINI_HTTP_ERROR", retryable=retryable)
        try:
            payload = response.json()
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMProviderError("Gemini returned invalid structured output.", code="GEMINI_RESULT_INVALID", retryable=True) from exc
        if not isinstance(result, dict):
            raise LLMProviderError("Gemini returned invalid structured output.", code="GEMINI_RESULT_INVALID", retryable=True)
        return result


class GeminiTranslationProvider(_GeminiBase):
    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(frozenset({("vi", "en"), ("en", "vi")}))

    async def translate_segments(self, segments: tuple[TranscriptionSegment, ...], source: str, target: str) -> tuple[TranscriptionSegment, ...]:
        if not segments:
            return ()
        batch_size = 35
        if len(segments) <= batch_size:
            return await self._translate_batch(segments, source, target)

        results: list[TranscriptionSegment] = []
        for i in range(0, len(segments), batch_size):
            chunk = segments[i : i + batch_size]
            translated_chunk = await self._translate_batch(chunk, source, target)
            results.extend(translated_chunk)
        return tuple(results)

    async def _translate_batch(self, segments: tuple[TranscriptionSegment, ...], source: str, target: str) -> tuple[TranscriptionSegment, ...]:
        schema: dict[str, object] = {"type": "object", "properties": {"translations": {"type": "array", "items": {"type": "string"}}}, "required": ["translations"]}
        prompt = f"Translate every transcript segment from {source} to {target}. Preserve order, meaning, names, numbers, and do not merge or omit segments. Return JSON only. Segments:\n" + json.dumps([item.text for item in segments], ensure_ascii=False)
        try:
            result = await self._generate(prompt, schema)
        except LLMProviderError as exc:
            raise TranslationProviderError(str(exc), code=exc.code, retryable=exc.retryable) from exc
        texts = result.get("translations")
        if not isinstance(texts, list) or len(texts) != len(segments) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise TranslationProviderError("Gemini returned invalid segment alignment.", code="TRANSLATION_RESULT_INVALID", retryable=False)
        return tuple(TranscriptionSegment(item.start_ms, item.end_ms, text.strip(), item.speaker_label, item.confidence) for item, text in zip(segments, texts, strict=True))


class GeminiSummaryProvider(_GeminiBase):
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(structured_output=True, max_context_tokens=None)

    async def generate_summary(
        self, transcript: str, template: str, output_language: str
    ) -> dict[str, object]:
        schema: dict[str, object] = {"type": "object", "properties": {"overview": {"type": "string"}, **{key: {"type": "array", "items": {"type": "string"}} for key in _SUMMARY_KEYS[1:]}}, "required": list(_SUMMARY_KEYS)}
        language_name = {"vi": "Vietnamese", "en": "English"}[output_language]
        prompt = (
            f"Create a faithful {template} meeting summary from the transcript. Never invent facts. "
            "For voting, emphasize decisions and votes; for briefing, make actions concise. "
            f"Write every human-readable value in {language_name}. Keep the JSON property names unchanged. "
            f"Return JSON only.\n\nTranscript:\n{transcript}"
        )
        return await self._generate(prompt, schema)


def build_gemini_translation_provider(settings: Settings) -> GeminiTranslationProvider:
    return GeminiTranslationProvider(
        settings.translation_api_key or settings.llm_api_key,
        settings.gemini_model,
        settings.gemini_api_base_url,
    )


def build_gemini_summary_provider(settings: Settings) -> GeminiSummaryProvider:
    return GeminiSummaryProvider(
        settings.llm_api_key or settings.translation_api_key,
        settings.llm_model or settings.gemini_model,
        settings.gemini_api_base_url,
    )
