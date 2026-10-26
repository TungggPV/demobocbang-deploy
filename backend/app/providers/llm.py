"""Backend-only registry for provider-neutral LLM adapters."""

from collections.abc import Callable

from app.core.config import Settings
from app.providers.contracts import LLMCapabilities, LLMProvider, LLMProviderError

LLMProviderFactory = Callable[[Settings], LLMProvider]


class LLMProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, LLMProviderFactory] = {}

    def register(self, name: str, factory: LLMProviderFactory) -> None:
        self._factories[name.strip().lower()] = factory

    def factory_for(self, name: str) -> LLMProviderFactory | None:
        return self._factories.get(name.strip().lower())

    def build(self, settings: Settings) -> LLMProvider | None:
        if not settings.llm_provider:
            return None
        factory = self._factories.get(settings.llm_provider.strip().lower())
        return factory(settings) if factory else None


class FallbackLLMProvider:
    """Try a secondary provider only after the primary provider returns an error."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self._primary, self._fallback = primary, fallback

    def capabilities(self) -> LLMCapabilities:
        primary = self._primary.capabilities()
        fallback = self._fallback.capabilities()
        return LLMCapabilities(
            structured_output=primary.structured_output or fallback.structured_output,
            max_context_tokens=primary.max_context_tokens or fallback.max_context_tokens,
        )

    async def polish_transcript(self, segments: tuple[str, ...], context: str) -> tuple[str, ...]:
        try:
            return await self._primary.polish_transcript(segments, context)
        except LLMProviderError:
            return await self._fallback.polish_transcript(segments, context)

    async def generate_summary(
        self,
        transcript: str,
        prompt: str | None,
        output_language: str,
        template: str | None = None,
    ) -> dict[str, object]:
        try:
            return await self._primary.generate_summary(
                transcript, prompt, output_language, template
            )
        except LLMProviderError:
            return await self._fallback.generate_summary(
                transcript, prompt, output_language, template
            )


llm_provider_registry = LLMProviderRegistry()

from app.providers.gemini import build_gemini_summary_provider  # noqa: E402
from app.providers.groq import build_groq_summary_provider  # noqa: E402

llm_provider_registry.register("gemini", build_gemini_summary_provider)
llm_provider_registry.register("groq", build_groq_summary_provider)


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    primary = llm_provider_registry.build(settings)
    if primary is None or not settings.llm_fallback_provider:
        return primary
    fallback_name = settings.llm_fallback_provider.strip().lower()
    primary_name = settings.llm_provider.strip().lower() if settings.llm_provider else ""
    if fallback_name == primary_name:
        raise ValueError("LLM_FALLBACK_PROVIDER must be different from LLM_PROVIDER.")
    factory = llm_provider_registry.factory_for(fallback_name)
    if factory is None:
        raise ValueError(f"Unsupported LLM fallback provider: {settings.llm_fallback_provider}.")
    return FallbackLLMProvider(primary, factory(settings))
