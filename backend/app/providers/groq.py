"""Groq structured-summary adapter using its OpenAI-compatible HTTP API."""

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


class _GroqBase:
    """Shared Groq API client for normalized AI providers."""

    def __init__(self, api_key: str | None, model: str, api_base_url: str) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is required when Groq is configured as an LLM provider.")
        self._key, self._model = api_key, model
        self._api_base_url = api_base_url.rstrip("/")

    async def _generate(self, prompt: str, schema: dict[str, object]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                response = await client.post(
                    f"{self._api_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": "Return only a JSON object that follows the supplied schema."},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {"name": "meeting_summary", "strict": True, "schema": schema},
                        },
                    },
                )
        except httpx.HTTPError as exc:
            raise LLMProviderError("Groq could not be reached.", code="GROQ_NETWORK_ERROR", retryable=True) from exc
        if response.is_error:
            provider_code = None
            try:
                payload = response.json()
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                provider_code = error.get("code") if isinstance(error, dict) else None
            except (TypeError, ValueError):
                pass
            if provider_code == "json_validate_failed":
                raise LLMProviderError(
                    "Groq could not generate valid structured output.",
                    code="GROQ_JSON_VALIDATE_FAILED",
                    retryable=True,
                )
            retryable = response.status_code == 429 or response.status_code >= 500
            raise LLMProviderError(
                "Groq rate limit was reached. Please try again shortly."
                if response.status_code == 429
                else "Groq rejected the request.",
                code="GROQ_RATE_LIMITED" if response.status_code == 429 else "GROQ_HTTP_ERROR",
                retryable=retryable,
            )
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMProviderError("Groq returned invalid structured output.", code="GROQ_RESULT_INVALID", retryable=True) from exc
        if not isinstance(result, dict):
            raise LLMProviderError("Groq returned invalid structured output.", code="GROQ_RESULT_INVALID", retryable=True)
        return result


class GroqSummaryProvider(TranscriptPolishMixin, _GroqBase):
    """Generate normalized meeting summaries through Groq."""

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(structured_output=True, max_context_tokens=131_072)

    async def generate_summary(
        self,
        transcript: str,
        user_prompt: str | None,
        output_language: str,
        template: str | None = None,
    ) -> dict[str, object]:
        language_name = {"vi": "Vietnamese", "en": "English"}[output_language]
        if template:
            schema: dict[str, object] = {
                "type": "object",
                "properties": {
                    "overview": {"type": "string"},
                    **{key: {"type": "array", "items": {"type": "string"}} for key in _SUMMARY_KEYS[1:]},
                },
                "required": list(_SUMMARY_KEYS),
                "additionalProperties": False,
            }
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
                "additionalProperties": False,
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


class GroqTranslationProvider(_GroqBase):
    """Translate transcript segments through Groq while preserving alignment."""

    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(frozenset({("vi", "en"), ("en", "vi")}))

    async def translate_segments(
        self, segments: tuple[TranscriptionSegment, ...], source: str, target: str
    ) -> tuple[TranscriptionSegment, ...]:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
            "required": ["translations"],
            "additionalProperties": False,
        }
        prompt = (
            f"Translate every transcript segment from {source} to {target}. Preserve order, meaning, "
            "names, numbers, and do not merge or omit segments. Return JSON only. Segments:\n"
            + json.dumps([item.text for item in segments], ensure_ascii=False)
        )
        try:
            result = await self._generate(prompt, schema)
        except LLMProviderError as exc:
            raise TranslationProviderError(str(exc), code=exc.code, retryable=exc.retryable) from exc
        texts = result.get("translations")
        if not isinstance(texts, list) or len(texts) != len(segments) or any(
            not isinstance(text, str) or not text.strip() for text in texts
        ):
            raise TranslationProviderError(
                "Groq returned invalid segment alignment.",
                code="TRANSLATION_RESULT_INVALID",
                retryable=False,
            )
        return tuple(
            TranscriptionSegment(item.start_ms, item.end_ms, text.strip(), item.speaker_label, item.confidence)
            for item, text in zip(segments, texts, strict=True)
        )


def build_groq_summary_provider(settings: Settings) -> GroqSummaryProvider:
    return GroqSummaryProvider(settings.groq_api_key, settings.groq_model, settings.groq_api_base_url)


def build_groq_translation_provider(settings: Settings) -> GroqTranslationProvider:
    return GroqTranslationProvider(settings.groq_api_key, settings.groq_model, settings.groq_api_base_url)
