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
    requested_top_k = int(args.get("top_k") or 8)
    top_k = max(6, min(requested_top_k, 12))
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
    visible_hits = hits[:6]
    lines = [
        f"针对「{query}」的混合检索结果（Milvus 语义召回 + Neo4j 图谱扩展，top {len(visible_hits)}/{len(hits)}）:"
    ]
    for i, h in enumerate(visible_hits, 1):
        snippet = (h.text or "").replace("\n", " ").strip()
        if len(snippet) > 450:
            snippet = snippet[:450] + "…"
        source = getattr(h, "source", "vector")
        lines.append(
            f"[{i}] source={source} score={h.score:.2f} chunk={h.chunk_id} · {snippet}"
        )
    if res.graph_facts:
        lines.append("关联实体:")
        for f in res.graph_facts[:8]:
            lines.append(
                f"- ({f.src_label}:{f.src_name}) -[{f.rel}]-> ({f.dst_label}:{f.dst_name})"
            )
    return "\n".join(lines)


kb_search = Tool(
    name="kb_search",
    description="查询企业内部知识库（Milvus 向量召回 + Neo4j 图谱扩展），返回可用于总结回答的多个片段。优先用它回答事实性问题。",
    params={
        "query": {"type": "string", "desc": "检索的中文短语，越具体越好"},
        "top_k": {"type": "int", "desc": "返回结果条数，默认 8；不要小于 6"},
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
    confidence = float(args.get("confidence") or 0.7)
    # Accept either a single `text` or a list `replies` — the agent picks
    # whichever fits the situation (1 polite ack vs. 3 short bubbles to
    # answer a multi-question dump).
    replies_raw = args.get("replies")
    replies: list[str] = []
    if isinstance(replies_raw, list):
        replies = [str(x).strip() for x in replies_raw if str(x).strip()]
    elif isinstance(replies_raw, str) and replies_raw.strip():
        replies = [replies_raw.strip()]
    text = (args.get("text") or "").strip()
    if text and not replies:
        replies = [text]
    if not replies:
        return "ERROR: 必须给出 text 或非空 replies 列表"
    if len(replies) > 6:
        return "ERROR: replies 最多 6 条；请合并"
    ctx.scratch["final_replies"] = replies
    ctx.scratch["final_text"] = replies[0]  # back-compat for callers
    ctx.scratch["final_confidence"] = max(0.0, min(1.0, confidence))
    return f"OK: 已记录 {len(replies)} 条最终回复"


final_reply = Tool(
    name="final_reply",
    description=(
        "给出对客户的最终中文回复并附置信度（0~1）。一次调用即结束本轮。"
        "默认用 `text` 单条回复；如果客户连发了多个独立问题、或你想把长答案拆成几段更自然的"
        "对话气泡，可改用 `replies` 列表（最多 6 条）。"
    ),
    params={
        "text": {"type": "string", "desc": "单条回复文本；如果用 replies 则可省略"},
        "replies": {"type": "list[string]", "desc": "多条回复（按发送顺序）。和 text 二选一"},
        "confidence": {"type": "float", "desc": "0~1，越高越确信。无 KB 支撑应 < 0.55"},
    },
    call=_final_reply,
)


def register_builtins() -> None:
    reg = get_registry()
    for t in (kb_search, set_profile_field, escalate_to_human, final_reply):
        reg.register(t)
