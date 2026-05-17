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
    "llm": {
        "provider", "model", "api_key", "base_url", "temperature", "supports_vision",
        "profiles", "active_profile", "fallback_profile", "fallback_enabled",
    },
    "embedding": {
        "provider", "model", "api_key", "base_url", "dim",
        "profiles", "active_profile",
    },
    "retrieval": {"top_k", "min_score"},
    "ai": {
        "confidence_threshold",
        "context_window",
        "default_prompt",
        "max_tokens",
        "agent_mode",
        "agent_max_steps",
        "react_force_llm",
        # Which persona (app/ai/personas/<id>/) shapes the conv_agent's
        # voice. Empty/unknown falls back to "default".
        "persona_id",
    },
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
            "supports_vision": settings.llm_supports_vision,
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
            "agent_mode": settings.agent_mode_enabled,
            "agent_max_steps": settings.conv_max_steps,
            "react_force_llm": settings.react_force_llm,
            "persona_id": "default",
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


def _default_profile_id(scope: str) -> str:
    return "default"


def _profile_scalar_keys(scope: str) -> tuple[str, ...]:
    if scope == "llm":
        return ("provider", "model", "api_key", "base_url", "temperature", "supports_vision")
    if scope == "embedding":
        return ("provider", "model", "api_key", "base_url", "dim")
    return ()


def _normalise_profiles(scope: str, value: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    if scope not in ("llm", "embedding"):
        return value

    scalar_keys = _profile_scalar_keys(scope)
    legacy_profile = {k: value.get(k, base.get(k)) for k in scalar_keys if value.get(k, base.get(k)) is not None}
    legacy_profile.setdefault("id", _default_profile_id(scope))
    legacy_profile.setdefault("name", "默认模型" if scope == "llm" else "默认向量模型")

    raw_profiles = value.get("profiles")
    profiles: list[dict[str, Any]] = []
    if isinstance(raw_profiles, list):
        for i, item in enumerate(raw_profiles):
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or f"profile_{i+1}").strip()
            if not pid:
                continue
            profile = {k: item.get(k) for k in scalar_keys if item.get(k) is not None}
            profile["id"] = pid
            profile["name"] = str(item.get("name") or pid)
            profiles.append(profile)

    if not profiles:
        profiles = [legacy_profile]

    active = str(value.get("active_profile") or profiles[0].get("id") or _default_profile_id(scope))
    if not any(p.get("id") == active for p in profiles):
        active = str(profiles[0].get("id") or _default_profile_id(scope))
    active_profile = next(p for p in profiles if p.get("id") == active)

    out = dict(base)
    out.update({k: active_profile.get(k) for k in scalar_keys if active_profile.get(k) is not None})
    out["profiles"] = profiles
    out["active_profile"] = active
    if scope == "llm":
        fallback = str(value.get("fallback_profile") or "")
        out["fallback_profile"] = fallback if any(p.get("id") == fallback for p in profiles) else ""
        out["fallback_enabled"] = bool(value.get("fallback_enabled", bool(out["fallback_profile"])))
    return out


async def get(db: AsyncSession, team_id: int, scope: str) -> dict[str, Any]:
    base = _env_defaults(scope)
    row = (
        await db.execute(
            select(TeamSetting).where(TeamSetting.team_id == team_id, TeamSetting.key == scope)
        )
    ).scalar_one_or_none()
    raw_value = {}
    if row and row.value_json:
        raw_value = dict(row.value_json)
        for k, v in row.value_json.items():
            if k in _ALLOWED.get(scope, ()) and v is not None and v != "":
                base[k] = v
    return _normalise_profiles(scope, raw_value or base, base)


async def get_profile(db: AsyncSession, team_id: int, scope: str, profile_id: str | None) -> dict[str, Any]:
    cfg = await get(db, team_id, scope)
    if scope not in ("llm", "embedding") or not profile_id:
        return cfg
    for profile in cfg.get("profiles") or []:
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            out = dict(cfg)
            for k in _profile_scalar_keys(scope):
                if profile.get(k) is not None:
                    out[k] = profile[k]
            out["active_profile"] = profile_id
            return out
    return cfg


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
