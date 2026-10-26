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
from app.providers.transcript_polish import TranscriptPolishMixin

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


class GeminiSummaryProvider(TranscriptPolishMixin, _GeminiBase):
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(structured_output=True, max_context_tokens=None)

    async def generate_summary(
        self,
        transcript: str,
        user_prompt: str | None,
        output_language: str,
        template: str | None = None,
    ) -> dict[str, object]:
        language_name = {"vi": "Vietnamese", "en": "English"}[output_language]
        if template:
            schema: dict[str, object] = {"type": "object", "properties": {"overview": {"type": "string"}, **{key: {"type": "array", "items": {"type": "string"}} for key in _SUMMARY_KEYS[1:]}}, "required": list(_SUMMARY_KEYS)}
            focus = {
                "general": "Cover the overall discussion and the most important outcomes.",
                "briefing": "Emphasize concise updates, blockers, owners, and next actions.",
                "voting": "Emphasize motions, voting results, decisions, and dissenting views.",
            }.get(template, "Cover the most important meeting outcomes.")
            prompt = (
                f"Create a faithful {template} meeting summary. {focus} "
                "Use only facts present in the transcript and never invent details. "
                f"Write every human-readable value in {language_name}. Keep the JSON property names unchanged. "
                f"Return JSON only.\n\nTranscript:\n{transcript}"
            )
        else:
            if not user_prompt:
                raise LLMProviderError(
                    "A custom summary prompt is required.", retryable=False
                )
            schema = {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                },
                "required": ["content"],
            }
            prompt = (
                "Create a faithful meeting summary that follows the user's requested structure and length. "
                "Place the entire result in the JSON content string. The content may contain plain-text "
                "headings, paragraphs, numbered lists, or bullet lists when the user asks for them. "
                "Use only facts present in the transcript and never invent details. "
                f"Write every human-readable value in {language_name}. Return JSON only. "
                f"\n\nUser request:\n{user_prompt}\n\nTranscript:\n{transcript}"
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
