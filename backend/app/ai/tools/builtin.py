"""Built-in tools available to the conversational agent.

Each tool returns a *string* observation. Keep them small and side-effect free
unless the side effect is the whole point (e.g. `set_profile_field`). The agent
prompt reads these names verbatim, so don't rename casually.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.kb.retriever import retrieve
from app.models import UserProfile

from . import Tool, ToolContext, get_registry

log = logging.getLogger(__name__)


# -------------------------------------------------------------- kb_search ----
async def _kb_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: query 不能为空"
    top_k = int(args.get("top_k") or 5)
    try:
        res = await retrieve(ctx.db, team_id=ctx.team_id, query=query, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        log.exception("kb_search failed")
        return f"ERROR: 检索失败 {e}"

    hits = list(res.hits)
    # accumulate hit ids so the workflow can broadcast them to the right panel
    bag = ctx.scratch.setdefault("kb_hit_ids", [])
    for h in hits:
        if h.chunk_id not in bag:
            bag.append(h.chunk_id)
    ctx.scratch["kb_context"] = res.to_context()

    if not hits:
        return f"未在知识库中找到与「{query}」相关的内容。"
    lines = [f"针对「{query}」的检索结果（top {len(hits)}）:"]
    for i, h in enumerate(hits, 1):
        snippet = (h.text or "").replace("\n", " ").strip()
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        lines.append(f"[{i}] score={h.score:.2f} chunk={h.chunk_id} · {snippet}")
    return "\n".join(lines)


kb_search = Tool(
    name="kb_search",
    description="查询企业内部知识库（向量+图谱检索），返回最相关的片段。优先用它回答事实性问题。",
    params={
        "query": {"type": "string", "desc": "检索的中文短语，越具体越好"},
        "top_k": {"type": "int", "desc": "返回结果条数，默认 5"},
    },
    call=_kb_search,
)


# ---------------------------------------------------- set_profile_field -----
async def _set_profile_field(ctx: ToolContext, args: dict[str, Any]) -> str:
    key = (args.get("key") or "").strip()
    value = args.get("value")
    if not key:
        return "ERROR: key 不能为空"
    if value is None:
        return "ERROR: value 不能为空"

    prof = await ctx.db.get(UserProfile, ctx.contact_id)
    if prof is None:
        # create on first write
        prof = UserProfile(contact_id=ctx.contact_id, team_id=ctx.team_id)
        ctx.db.add(prof)
        await ctx.db.flush()
    prefs = dict(prof.preferences_json or {})
    prefs[key] = value
    prof.preferences_json = prefs
    await ctx.db.commit()
    return f"已记录客户画像：{key} = {json.dumps(value, ensure_ascii=False)}"


set_profile_field = Tool(
    name="set_profile_field",
    description="把客户在本轮对话里透露的偏好/事实写入长期画像（如：行业、兴趣、预算）。键名要稳定可复用。",
    params={
        "key": {"type": "string", "desc": "字段名，例如 industry / budget / role"},
        "value": {"type": "any", "desc": "字段值，字符串或数字"},
    },
    call=_set_profile_field,
)


# ------------------------------------------------------- escalate_to_human --
async def _escalate(ctx: ToolContext, args: dict[str, Any]) -> str:
    reason = (args.get("reason") or "").strip() or "需要人工介入"
    ctx.scratch["escalate"] = True
    ctx.scratch["escalate_reason"] = reason
    return f"已标记需要人工介入：{reason}"


escalate_to_human = Tool(
    name="escalate_to_human",
    description="当你无法可靠回答、客户明确要求人工、或涉及敏感事务（投诉/退款/合规）时，调用本工具转人工。",
    params={
        "reason": {"type": "string", "desc": "一句话说明为什么要转人工"},
    },
    call=_escalate,
)


# ------------------------------------------------------- final_reply (done) -
async def _final_reply(ctx: ToolContext, args: dict[str, Any]) -> str:
    text = (args.get("text") or "").strip()
    confidence = float(args.get("confidence") or 0.7)
    if not text:
        return "ERROR: text 不能为空"
    ctx.scratch["final_text"] = text
    ctx.scratch["final_confidence"] = max(0.0, min(1.0, confidence))
    return "OK: 最终回复已记录"


final_reply = Tool(
    name="final_reply",
    description="给出对客户的最终中文回复，并附置信度（0~1）。一旦调用本工具，本轮结束。",
    params={
        "text": {"type": "string", "desc": "给客户看的回复文本"},
        "confidence": {"type": "float", "desc": "0~1，越高越确信。无 KB 支撑应 < 0.55"},
    },
    call=_final_reply,
)


def register_builtins() -> None:
    reg = get_registry()
    for t in (kb_search, set_profile_field, escalate_to_human, final_reply):
        reg.register(t)
