"""LLM provider factory — picks the implementation from settings."""

from __future__ import annotations

from ..config import Settings
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .openai_provider import OpenAIProvider


def build_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty.")
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model)
    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is empty.")
        return OpenAIProvider(settings.openai_api_key, settings.openai_model)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}. Expected 'anthropic' or 'openai'."
    )
