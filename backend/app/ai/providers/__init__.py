"""LLM provider abstraction.

Pick the provider via `LLM_PROVIDER` env var; falls back to `mock` so the
system runs without any external credentials (great for dev / CI / demo).
"""
from __future__ import annotations

from app.core.config import settings

from .base import ChatMessage, LLMProvider, LLMResult
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["ChatMessage", "LLMProvider", "LLMResult", "get_provider"]

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is not None:
        return _provider
    name = settings.llm_provider.lower()
    if name == "openai":
        _provider = OpenAICompatibleProvider(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or "https://api.openai.com/v1",
            model=settings.llm_model,
        )
    else:
        _provider = MockProvider()
    return _provider
