"""Team-scoped runtime settings.

Loaded on demand from `team_settings` and merged on top of env defaults from
`app.core.config.settings`. Each scope ("llm" / "embedding" / "retrieval" /
"ai") has its own row.

We expose:
  - get(db, team_id, scope) -> dict   (merged: db over env)
  - upsert(db, team_id, scope, value) -> int (new version)
  - version(db, team_id, scope) -> int (used as cache key)

Providers (LLM / embedding / vector / graph) consult these for live config so
the user can change the model from the Web UI without restarting the backend.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import TeamSetting

# Allowed keys per scope. Anything else is silently dropped — never let UI
# inject arbitrary attributes (defence-in-depth, the schema layer is the
# primary gate).
_ALLOWED: dict[str, set[str]] = {
    "llm": {"provider", "model", "api_key", "base_url", "temperature"},
    "embedding": {"provider", "model", "api_key", "base_url", "dim"},
    "retrieval": {"top_k", "min_score"},
    "ai": {"confidence_threshold", "context_window", "default_prompt", "max_tokens"},
    "parser": {
        "backend",
        "api_base",
        "api_key",
        "model_version",
        "local_cmd",
        "local_extra_args",
    },
}


def _env_defaults(scope: str) -> dict[str, Any]:
    if scope == "llm":
        return {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "base_url": settings.llm_base_url,
            "temperature": settings.llm_temperature,
        }
    if scope == "embedding":
        return {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "api_key": settings.embedding_api_key,
            "base_url": settings.embedding_base_url,
            "dim": settings.embedding_dim,
        }
    if scope == "retrieval":
        return {"top_k": settings.kb_top_k, "min_score": settings.kb_min_score}
    if scope == "ai":
        return {
            "confidence_threshold": settings.ai_confidence_threshold,
            "context_window": settings.ai_context_window,
            "default_prompt": settings.ai_default_prompt,
            "max_tokens": settings.ai_max_tokens,
        }
    if scope == "parser":
        return {
            "backend": settings.parser_backend,
            "api_base": settings.mineru_api_base,
            "api_key": settings.mineru_api_token,
            "model_version": settings.mineru_model_version,
            "local_cmd": settings.mineru_local_cmd,
            "local_extra_args": settings.mineru_local_extra_args,
        }
    return {}


async def get(db: AsyncSession, team_id: int, scope: str) -> dict[str, Any]:
    base = _env_defaults(scope)
    row = (
        await db.execute(
            select(TeamSetting).where(TeamSetting.team_id == team_id, TeamSetting.key == scope)
        )
    ).scalar_one_or_none()
    if row and row.value_json:
        for k, v in row.value_json.items():
            if k in _ALLOWED.get(scope, ()) and v is not None and v != "":
                base[k] = v
    return base


async def version(db: AsyncSession, team_id: int, scope: str) -> int:
    row = (
        await db.execute(
            select(TeamSetting).where(TeamSetting.team_id == team_id, TeamSetting.key == scope)
        )
    ).scalar_one_or_none()
    return row.version if row else 0


async def upsert(db: AsyncSession, team_id: int, scope: str, value: dict[str, Any]) -> int:
    allowed = _ALLOWED.get(scope, set())
    filtered = {k: v for k, v in value.items() if k in allowed}
    row = (
        await db.execute(
            select(TeamSetting).where(TeamSetting.team_id == team_id, TeamSetting.key == scope)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TeamSetting(team_id=team_id, key=scope, value_json=filtered, version=1)
        db.add(row)
    else:
        # preserve fields not present in the new payload
        merged = {**(row.value_json or {}), **filtered}
        row.value_json = merged
        row.version += 1
    await db.commit()
    await db.refresh(row)
    return row.version


def has_api_key(merged: dict[str, Any]) -> bool:
    return bool((merged.get("api_key") or "").strip())
