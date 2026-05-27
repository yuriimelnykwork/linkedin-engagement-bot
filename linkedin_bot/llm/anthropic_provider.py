"""Anthropic Claude provider."""

from __future__ import annotations

from anthropic import Anthropic

from .base import LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        chunks: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        return "".join(chunks).strip()
