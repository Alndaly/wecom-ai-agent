"""Embedding provider factory — team-aware (mirrors LLM factory).

Resolution: DB team_settings("embedding") → env defaults → mock.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

from .base import EmbeddingProvider
from .mock import MockEmbedding
from .openai_compatible import OpenAIEmbedding

__all__ = [
    "EmbeddingProvider",
    "get_embedding_provider",
    "build_embedding_provider",
    "reset_cache",
]

_cache: dict[tuple[int, int, str], EmbeddingProvider] = {}
log = logging.getLogger(__name__)


def build_embedding_provider(cfg: dict) -> EmbeddingProvider:
    name = (cfg.get("provider") or "mock").lower()
    api_key = (cfg.get("api_key") or "").strip()
    if name == "openai" and api_key:
        return OpenAIEmbedding(
            api_key=api_key,
            base_url=(cfg.get("base_url") or "").strip() or "https://api.openai.com/v1",
            model=cfg.get("model") or "text-embedding-3-small",
            dim=int(cfg.get("dim") or 1536),
        )
    return MockEmbedding(dim=int(cfg.get("dim") or 256))


async def get_embedding_provider(db: AsyncSession, team_id: int) -> EmbeddingProvider:
    cfg = await settings_service.get(db, team_id, "embedding")
    ver = await settings_service.version(db, team_id, "embedding")
    profile = str(cfg.get("active_profile") or "default")
    key = (team_id, ver, profile)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    inst = build_embedding_provider(cfg)
    _cache[key] = inst
    log.info("embedding provider built team=%s profile=%s provider=%s", team_id, profile, getattr(inst, "name", "?"))
    return inst


def reset_cache(team_id: int | None = None) -> None:
    if team_id is None:
        _cache.clear()
    else:
        for k in [k for k in _cache if k[0] == team_id]:
            _cache.pop(k, None)
