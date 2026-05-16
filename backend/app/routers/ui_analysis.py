from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps import current_user
from app.models import User
from app.services import settings_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ui-analysis", tags=["ui-analysis"])


class UiAnalysisIn(BaseModel):
    contact_name: str = ""
    current_page: str = "UNKNOWN"
    tree: str = ""
    image: str | None = None
    mime: str = "image/jpeg"


class UiAnalysisOut(BaseModel):
    ok: bool
    page: Literal["home", "search", "chat", "contact", "other"] = "other"
    target_matches: bool = False
    confidence: float = 0.0
    reason: str = ""
    suggested_action: str = "none"
    latency_ms: int | None = None
    error: str | None = None
    raw: str | None = None


def _strip_json(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            s = s.split("\n", 1)[1]
    return s.strip()


def _data_url(image_b64: str | None, mime: str) -> str | None:
    if not image_b64:
        return None
    return f"data:{mime};base64,{image_b64}"


@router.post("", response_model=UiAnalysisOut)
async def analyze_ui(
    body: UiAnalysisIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> UiAnalysisOut:
    llm = await settings_service.get(db, user.team_id, "llm")
    provider = (llm.get("provider") or "mock").lower()
    api_key = (llm.get("api_key") or "").strip()
    base_url = (llm.get("base_url") or settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
    model = llm.get("model") or settings.llm_model

    if provider != "openai" or not api_key:
        return UiAnalysisOut(
            ok=False,
            error="ui analysis requires provider=openai with api_key",
        )

    system = (
        "你在判断企业微信页面状态。"
        "请只输出严格 JSON，不要输出多余文字。"
        "格式："
        '{"page":"home|search|chat|contact|other",'
        '"target_matches":true|false,'
        '"confidence":0.0,'
        '"reason":"简短原因",'
        '"suggested_action":"back|open_search|type_search|tap_target_chat|type_in_chat|send|none"}。'
        "如果当前页面已经是目标联系人聊天页，page=chat 且 target_matches=true。"
    )
    user_text = (
        f"目标联系人: {body.contact_name}\n"
        f"当前页面: {body.current_page}\n"
        f"UI树:\n{body.tree}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if body.image:
        content.append({"type": "image_url", "image_url": {"url": _data_url(body.image, body.mime)}})
        messages[1]["content"] = content

    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=60) as http:
        resp = await http.post(
            f"{base_url}/chat/completions",
            json=request,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if resp.status_code >= 400:
        return UiAnalysisOut(
            ok=False,
            error=f"LLM HTTP {resp.status_code}: {resp.text}",
            latency_ms=latency_ms,
        )

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001
        return UiAnalysisOut(ok=False, error=f"unexpected LLM response: {e}", latency_ms=latency_ms)

    try:
        parsed = json.loads(_strip_json(text))
    except Exception as e:  # noqa: BLE001
        log.warning("ui analysis parse failed: %s", text)
        return UiAnalysisOut(ok=False, error=f"parse failed: {e}", raw=text, latency_ms=latency_ms)

    return UiAnalysisOut(
        ok=True,
        page=str(parsed.get("page") or "other"),
        target_matches=bool(parsed.get("target_matches", False)),
        confidence=float(parsed.get("confidence") or 0.0),
        reason=str(parsed.get("reason") or ""),
        suggested_action=str(parsed.get("suggested_action") or "none"),
        latency_ms=latency_ms,
    )
