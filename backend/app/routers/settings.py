"""Runtime settings — team-scoped, hot-reloadable from the Web UI.

Scopes:
  - llm        provider / model / api_key / base_url / temperature
  - embedding  provider / model / api_key / base_url / dim
  - retrieval  top_k / min_score
  - ai         confidence_threshold / context_window / default_prompt

`api_key` is write-only: GET returns a masked placeholder so we never leak
secrets back to the browser; PUT only updates the key if a non-empty value
is provided.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import build_provider, reset_cache as reset_llm_cache
from app.ai.providers.base import ChatMessage
from app.core.db import get_db
from app.deps import current_user
from app.kb.embeddings import (
    build_embedding_provider,
    reset_cache as reset_embedding_cache,
)
from app.kb.vectorstore import get_vector_store
from app.kb.graphstore import get_graph_store
from app.core.config import settings as env_settings
from app.models import User
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["settings"])

Scope = Literal["llm", "embedding", "retrieval", "ai", "parser"]


# ---------- masking ----------
_MASK = "********"


def _mask_api_key(d: dict) -> dict:
    out = dict(d)
    if "api_key" in out:
        out["api_key"] = _MASK if out["api_key"] else ""
    if isinstance(out.get("profiles"), list):
        masked = []
        for profile in out["profiles"]:
            if not isinstance(profile, dict):
                continue
            p = dict(profile)
            p["api_key"] = _MASK if p.get("api_key") else ""
            masked.append(p)
        out["profiles"] = masked
    return out


def _is_placeholder(val: str | None) -> bool:
    """Empty or the literal mask sent back from the UI = 'keep saved value'."""
    return not val or val == _MASK


# ---------- schemas ----------
class LLMIn(BaseModel):
    provider: Literal["mock", "openai"] = "openai"
    model: str = Field(default="gpt-4o-mini", max_length=128)
    # Empty string means "do not change" on PUT.
    api_key: str = ""
    base_url: str = ""
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # Tick this when the configured model accepts inline images (gpt-4o / qwen-vl
    # / glm-4v). The ReAct device agent attaches the current screenshot when on.
    supports_vision: bool = False
    profiles: list[dict[str, Any]] = Field(default_factory=list)
    active_profile: str = "default"
    fallback_profile: str = ""
    fallback_enabled: bool = False


class EmbeddingIn(BaseModel):
    provider: Literal["mock", "openai"] = "openai"
    model: str = Field(default="text-embedding-3-small", max_length=128)
    api_key: str = ""
    base_url: str = ""
    dim: int = Field(default=1536, ge=16, le=8192)
    profiles: list[dict[str, Any]] = Field(default_factory=list)
    active_profile: str = "default"


class RetrievalIn(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.25, ge=0.0, le=1.0)


class AIBehaviorIn(BaseModel):
    confidence_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    context_window: int = Field(default=10, ge=1, le=50)
    default_prompt: str = ""
    max_tokens: int = Field(default=8192, ge=64, le=32768)
    agent_mode: bool = True
    agent_max_steps: int = Field(default=5, ge=1, le=20)
    # 决策模式：False = 规则快路径优先、LLM 兜底（默认）；True = 每一步都走 LLM。
    # AI 都只能选节点 ID，由后端解析坐标（不会让 AI 猜 x/y）。
    react_force_llm: bool = False
    # 选用哪一份人格（对应 backend/app/ai/personas/<id>/）。
    # 空 / 无效值会自动 fallback 到 "default"。
    persona_id: str = ""


class ParserIn(BaseModel):
    backend: Literal["builtin", "mineru_local", "mineru_api"] = "builtin"
    api_base: str = "https://mineru.net/api/v4"
    # Empty / "********" means "keep saved value" (same as llm.api_key).
    api_key: str = ""
    model_version: Literal["vlm", "pipeline"] = "vlm"
    local_cmd: str = "mineru"
    local_extra_args: str = ""


# ---------- read all ----------
@router.get("")
async def read_all(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    out = {}
    for scope in ("llm", "embedding", "retrieval", "ai", "parser"):
        v = await settings_service.get(db, user.team_id, scope)
        if scope in ("llm", "embedding", "parser"):
            v = _mask_api_key(v)
        out[scope] = v
    # also surface the read-only infra config so the UI can show it
    out["infra"] = {
        "vector_store": env_settings.vector_store,
        "graph_store": env_settings.graph_store,
        "milvus_uri": env_settings.milvus_uri,
        "milvus_collection": env_settings.milvus_collection,
        "neo4j_uri": env_settings.neo4j_uri,
    }
    return out


# ---------- write per-scope ----------
async def _upsert(
    db: AsyncSession,
    team_id: int,
    scope: Scope,
    payload: dict,
    *,
    treat_placeholder_as_keep: bool = True,
) -> int:
    # api_key empty or still the mask "********" → drop, so the existing one is preserved
    if (
        treat_placeholder_as_keep
        and "api_key" in payload
        and _is_placeholder(payload["api_key"])
    ):
        payload = {k: v for k, v in payload.items() if k != "api_key"}
    if treat_placeholder_as_keep and isinstance(payload.get("profiles"), list):
        saved = await settings_service.get(db, team_id, scope)
        saved_profiles = {
            str(p.get("id")): p
            for p in (saved.get("profiles") or [])
            if isinstance(p, dict) and p.get("id")
        }
        profiles = []
        for profile in payload["profiles"]:
            if not isinstance(profile, dict):
                continue
            p = dict(profile)
            old = saved_profiles.get(str(p.get("id")))
            if _is_placeholder(p.get("api_key")):
                if old and old.get("api_key"):
                    p["api_key"] = old["api_key"]
                else:
                    p.pop("api_key", None)
            profiles.append(p)
        payload = {**payload, "profiles": profiles}
    return await settings_service.upsert(db, team_id, scope, payload)


@router.put("/llm")
async def write_llm(
    body: LLMIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "llm", body.model_dump())
    reset_llm_cache(user.team_id)
    return {"version": ver}


@router.put("/embedding")
async def write_embedding(
    body: EmbeddingIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "embedding", body.model_dump())
    reset_embedding_cache(user.team_id)
    return {"version": ver}


@router.put("/retrieval")
async def write_retrieval(
    body: RetrievalIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "retrieval", body.model_dump())
    return {"version": ver}


@router.put("/ai")
async def write_ai_behavior(
    body: AIBehaviorIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ver = await _upsert(db, user.team_id, "ai", body.model_dump())
    return {"version": ver}


@router.put("/parser")
async def write_parser(
    body: ParserIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ver = await _upsert(db, user.team_id, "parser", body.model_dump())
    return {"version": ver}


# ---------- probes ----------
class ProbeOut(BaseModel):
    ok: bool
    detail: str
    latency_ms: int | None = None
    model: str | None = None
    dim: int | None = None


def _merge_for_test(saved: dict, body_payload: dict | None) -> dict:
    """Overlay body on top of saved, with placeholder api_key ('' or '********')
    meaning 'use saved value'."""
    cfg = dict(saved)
    if body_payload is None:
        return cfg
    for k, v in body_payload.items():
        if k == "api_key" and _is_placeholder(v):
            continue  # keep saved api_key
        cfg[k] = v
    if isinstance(cfg.get("profiles"), list):
        active = str(cfg.get("active_profile") or "")
        for profile in cfg["profiles"]:
            if isinstance(profile, dict) and str(profile.get("id") or "") == active:
                for pk, pv in profile.items():
                    if pk == "api_key" and _is_placeholder(pv):
                        continue
                    cfg[pk] = pv
                break
    return cfg


def _requires_real_key(cfg: dict) -> bool:
    return (cfg.get("provider") or "").lower() == "openai"


@router.post("/test/llm", response_model=ProbeOut)
async def test_llm(
    body: LLMIn | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ProbeOut:
    """One-shot ping. If a body is provided, the form values are tested
    *without* persisting; otherwise the currently-saved config is used.
    api_key="" or "********" in the body means 'use saved value'.

    When the user explicitly picked provider=openai but no usable api_key can
    be resolved, we fail loudly — silent fallback to mock used to make people
    think their real-model config worked.
    """
    saved = await settings_service.get(db, user.team_id, "llm")
    cfg = _merge_for_test(saved, body.model_dump() if body else None)

    if _requires_real_key(cfg) and not (cfg.get("api_key") or "").strip():
        return ProbeOut(
            ok=False,
            detail="provider=openai 但 api_key 为空（请填写 api_key 后再点测试）",
            model="(none)",
        )

    try:
        provider = build_provider(cfg)
        result = await provider.chat(
            [ChatMessage(role="user", content="ping")],
            temperature=0.0,
            max_tokens=16,
        )
        # Surface which provider actually answered so the user can spot a fallback.
        prov_name = getattr(provider, "name", "?")
        actual = f"{prov_name} · {result.model}"
        # If user asked for openai but we still got mock (defensive), flag it.
        if _requires_real_key(cfg) and prov_name != "openai":
            return ProbeOut(
                ok=False,
                detail=f"配置 provider=openai 但实际使用了 {prov_name}（检查 api_key / base_url）",
                model=actual,
            )
        return ProbeOut(
            ok=True,
            detail=result.text or "(empty)",
            latency_ms=result.latency_ms,
            model=actual,
        )
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e))


@router.post("/test/embedding", response_model=ProbeOut)
async def test_embedding(
    body: EmbeddingIn | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ProbeOut:
    saved = await settings_service.get(db, user.team_id, "embedding")
    cfg = _merge_for_test(saved, body.model_dump() if body else None)

    if _requires_real_key(cfg) and not (cfg.get("api_key") or "").strip():
        return ProbeOut(
            ok=False,
            detail="provider=openai 但 api_key 为空（请填写 api_key 后再点测试）",
        )

    try:
        provider = build_embedding_provider(cfg)
        vec = await provider.embed_one("ping")
        prov_name = getattr(provider, "name", "?")
        model = getattr(provider, "model", provider.name)
        actual = f"{prov_name} · {model}"
        if _requires_real_key(cfg) and prov_name != "openai":
            return ProbeOut(
                ok=False,
                detail=f"配置 provider=openai 但实际使用了 {prov_name}（检查 api_key / base_url）",
                model=actual,
                dim=len(vec),
            )
        return ProbeOut(
            ok=True,
            detail=f"vector returned, |v|={len(vec)}",
            model=actual,
            dim=len(vec),
        )
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e))


@router.post("/test/vector_store", response_model=ProbeOut)
async def test_vector_store(user: User = Depends(current_user)) -> ProbeOut:
    try:
        store = get_vector_store()
        # round-trip a single zero vector under a sentinel meta
        dim = 8
        await store.upsert(
            ["__probe__"],
            [[0.0] * dim],
            [{"team_id": -1, "kb_id": -1, "doc_id": -1, "chunk_id": -1, "text": "probe"}],
        )
        await store.delete_by_meta("team_id", -1)
        return ProbeOut(ok=True, detail=f"backend={store.name}")
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e))


@router.post("/test/parser", response_model=ProbeOut)
async def test_parser(
    body: ParserIn | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ProbeOut:
    """Probe the configured document parser.

    For mineru_local we just check the binary exists (--version). For
    mineru_api we hit a cheap endpoint with the bearer token. builtin is
    always OK.
    """
    import asyncio
    import shlex
    import time

    saved = await settings_service.get(db, user.team_id, "parser")
    cfg = _merge_for_test(saved, body.model_dump() if body else None)
    backend = (cfg.get("backend") or "builtin").strip()
    started = time.monotonic()

    if backend == "builtin":
        return ProbeOut(ok=True, detail="builtin: text + pypdf", model="builtin")

    if backend == "mineru_local":
        cmd = (cfg.get("local_cmd") or "mineru").strip()
        try:
            argv = [*shlex.split(cmd), "--version"]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                err = (stderr or stdout or b"").decode("utf-8", errors="replace")
                return ProbeOut(ok=False, detail=f"`{cmd} --version` exit={proc.returncode}: {err}")
            ver = (stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
            head = ver[0] if ver else "(no output)"
            return ProbeOut(
                ok=True,
                detail=head,
                model=f"mineru_local · {cmd}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except FileNotFoundError:
            return ProbeOut(ok=False, detail=f"未找到命令: {cmd}（请先 pip install -U mineru[all]）")
        except Exception as e:  # noqa: BLE001
            return ProbeOut(ok=False, detail=str(e))

    if backend == "mineru_api":
        token = (cfg.get("api_key") or "").strip()
        if not token:
            return ProbeOut(ok=False, detail="api_key 为空（请填写 mineru.net 的 Bearer Token）")
        api_base = (cfg.get("api_base") or env_settings.mineru_api_base).rstrip("/")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Cheap call: just submitting an empty `files` list returns a
                # validation error from the server, which is enough to verify
                # auth + connectivity. A 200 with code != 0 means auth worked.
                r = await client.post(
                    f"{api_base}/file-urls/batch",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "files": [{"name": "probe.pdf"}],
                        "model_version": cfg.get("model_version") or "vlm",
                    },
                )
            if r.status_code == 401:
                return ProbeOut(ok=False, detail="401 unauthorized（token 无效或已过期）")
            if r.status_code >= 500:
                return ProbeOut(ok=False, detail=f"{r.status_code} {r.text}")
            env = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            code = env.get("code")
            msg = env.get("msg") or env.get("message") or ""
            ok = code in (0, 200) or "ok" in str(msg).lower()
            return ProbeOut(
                ok=bool(ok),
                detail=f"code={code} msg={msg or '(empty)'}",
                model=f"mineru_api · {cfg.get('model_version') or 'vlm'}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return ProbeOut(ok=False, detail=str(e))

    return ProbeOut(ok=False, detail=f"unknown backend: {backend}")


@router.post("/test/graph_store", response_model=ProbeOut)
async def test_graph_store(user: User = Depends(current_user)) -> ProbeOut:
    try:
        store = get_graph_store()
        return ProbeOut(ok=True, detail=f"backend={store.name}")
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e))
