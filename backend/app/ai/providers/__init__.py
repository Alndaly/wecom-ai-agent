"""LLM provider factory.

Resolution order for each team:
    DB team_settings("llm")  →  env defaults  →  built-in mock

The factory is cached per (team_id, version). When the user saves new
settings via /settings, `version` bumps and the next call rebuilds the
provider — no backend restart required.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

from .base import ChatMessage, LLMProvider, LLMResult
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["ChatMessage", "LLMProvider", "LLMResult", "get_provider", "build_provider"]

# (team_id, version) → provider
_cache: dict[tuple[int, int], LLMProvider] = {}


def build_provider(cfg: dict) -> LLMProvider:
    """Construct a provider from a merged config dict.

    `provider`:
      - "openai"  : OpenAI / DeepSeek / Qwen / Ollama-v1 ... any chat-completions
      - anything else (incl. "mock", "" , "auto"): MockProvider
    """
    name = (cfg.get("provider") or "mock").lower()
    api_key = (cfg.get("api_key") or "").strip()
    if name == "openai" and api_key:
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=(cfg.get("base_url") or "").strip() or "https://api.openai.com/v1",
            model=cfg.get("model") or "gpt-4o-mini",
        )
    # graceful fallback: explicit "openai" without api key → mock with warning
    return MockProvider()


async def get_provider(db: AsyncSession, team_id: int) -> LLMProvider:
    cfg = await settings_service.get(db, team_id, "llm")
    ver = await settings_service.version(db, team_id, "llm")
    key = (team_id, ver)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    inst = build_provider(cfg)
    _cache[key] = inst
    return inst


def reset_cache(team_id: int | None = None) -> None:
    if team_id is None:
        _cache.clear()
    else:
        for k in [k for k in _cache if k[0] == team_id]:
            _cache.pop(k, None)
