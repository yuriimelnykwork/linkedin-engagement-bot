"""LLM provider abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Minimal text-in / text-out interface so the rest of the codebase
    is agnostic about which provider (Anthropic, OpenAI, ...) is used."""

    name: str = "abstract"
    model: str = ""

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        """Return the assistant's text reply. Raises on hard failure."""
