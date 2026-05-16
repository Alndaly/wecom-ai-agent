"""LLM provider factory.

Resolution order for each team:
    DB team_settings("llm")  →  env defaults  →  built-in mock

The factory is cached per (team_id, version). When the user saves new
settings via /settings, `version` bumps and the next call rebuilds the
provider — no backend restart required.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import settings_service

from .base import ChatMessage, LLMProvider, LLMResult
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "ChatMessage",
    "LLMProvider",
    "LLMResult",
    "get_provider",
    "get_fallback_provider",
    "build_provider",
]

log = logging.getLogger(__name__)

# (team_id, version, profile_id) → provider
_cache: dict[tuple[int, int, str], LLMProvider] = {}

# Per-team semaphore caps simultaneous LLM chat calls so a burst of concurrent
# conversations doesn't blow through provider rate limits or budget. The
# downstream device queue stays serial, so the semaphore only governs how many
# `handle_inbound` LLM phases overlap.
_chat_semaphores: dict[int, asyncio.Semaphore] = {}


def _get_chat_semaphore(team_id: int) -> asyncio.Semaphore:
    sem = _chat_semaphores.get(team_id)
    if sem is None:
        sem = asyncio.Semaphore(max(1, int(settings.llm_max_concurrent)))
        _chat_semaphores[team_id] = sem
    return sem


class _SemaphoredProvider(LLMProvider):
    """Decorator that serializes `chat()` through a team-wide semaphore.

    All other attributes (name, model, embedding helpers, …) delegate to the
    wrapped provider, so callers can't tell the difference.
    """

    def __init__(self, inner: LLMProvider, team_id: int) -> None:
        self._inner = inner
        self._team_id = team_id
        self.name = inner.name

    def __getattr__(self, item):  # delegate everything we didn't override
        return getattr(self._inner, item)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> LLMResult:
        async with _get_chat_semaphore(self._team_id):
            return await self._inner.chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )


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
    return await get_profile_provider(db, team_id, str(cfg.get("active_profile") or ""))


async def get_fallback_provider(db: AsyncSession, team_id: int) -> LLMProvider | None:
    cfg = await settings_service.get(db, team_id, "llm")
    if not cfg.get("fallback_enabled"):
        return None
    fallback_id = str(cfg.get("fallback_profile") or "")
    active_id = str(cfg.get("active_profile") or "")
    if not fallback_id or fallback_id == active_id:
        return None
    return await get_profile_provider(db, team_id, fallback_id)


async def get_profile_provider(db: AsyncSession, team_id: int, profile_id: str) -> LLMProvider:
    cfg = await settings_service.get_profile(db, team_id, "llm", profile_id)
    ver = await settings_service.version(db, team_id, "llm")
    key = (team_id, ver, str(cfg.get("active_profile") or profile_id or "default"))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    raw = build_provider(cfg)
    inst: LLMProvider = _SemaphoredProvider(raw, team_id)
    _cache[key] = inst
    log.info("llm provider built team=%s profile=%s provider=%s model=%s", team_id, key[2], inst.name, getattr(inst, "model", "?"))
    return inst


def reset_cache(team_id: int | None = None) -> None:
    if team_id is None:
        _cache.clear()
        _chat_semaphores.clear()
    else:
        for k in [k for k in _cache if k[0] == team_id]:
            _cache.pop(k, None)
        _chat_semaphores.pop(team_id, None)
