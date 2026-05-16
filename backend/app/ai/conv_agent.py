"""Conversational ReAct agent — replaces the single-shot `_generate` path
in `workflow.py` when `agent_mode_enabled=True`.

Loop (bounded by `max_steps`):
  1. Render system prompt with tool catalogue + customer profile + history
  2. Ask LLM to emit JSON `{thought, tool, args}` OR `{thought, tool: "final_reply", ...}`
  3. Execute the tool, append observation to running context
  4. If `final_reply` was called, stop

Every step is logged (info-level + AIReplyLog row) so post-mortems are
straightforward.

Designed to be drop-in compatible with the existing `Decision` shape used by
`workflow.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.ai.providers import ChatMessage
from app.ai.tools import ToolContext, get_registry

# Per-step LLM timeout. If the upstream provider hangs we MUST not block the
# inbound-message pipeline indefinitely — the customer is waiting on a reply.
_STEP_LLM_TIMEOUT_SEC = 45.0
# Per-tool execution timeout — protects against a misbehaving skill / MCP tool.
_TOOL_TIMEOUT_SEC = 20.0

log = logging.getLogger(__name__)


@dataclass
class AgentReplyResult:
    text: str | None
    confidence: float
    model: str
    latency_ms: int
    steps: int
    replies: list[str] | None = None  # multi-bubble final answer when agent chose to split
    escalate: bool = False
    escalate_reason: str = ""
    kb_hit_ids: list[int] | None = None
    kb_context: str = ""
    trace: list[dict[str, Any]] | None = None


_SYSTEM_TEMPLATE = """你是企业的私域客服智能体。请用工具一步步推理来回答客户问题。

【系统设定】
{system}

【客户画像】
{profile}

【可用工具】
{tools}

【输出协议】
- 每一步只输出严格 JSON：{{"thought": "中文思考...", "tool": "工具名", "args": {{...}}}}
- 不要把 JSON 包在 ```代码块``` 里，不要在 JSON 之外多说一个字。

【关于知识库检索】
- 优先使用客户原话作为 query 调一次 kb_search（命中率最高）；不要把 top_k 设得过小，通常保持默认或 ≥ 6。
- kb_search 会做 Milvus 语义召回 + Neo4j 图谱扩展。回答时要综合多个片段总结，不要只根据目录、页码、章节标题类片段下结论。
- 如果第一次只返回目录/页码/标题，或内容不足以回答，**再换 1~2 种措辞重试**（拆关键词、用同义说法、繁简体关键词都可以）。
- 命中知识库片段时，回复必须紧扣片段内容，不要凭空发挥；并把 confidence 设到 ≥ 0.7。
- **回复必须是完整的句子**，不要用 "..."、"…"、"等等" 这类省略号/省略表达结尾。如果一条说不完，用 replies 拆成多条。

【何时调用 escalate_to_human（重要）】
- 客户明确要求「人工/转人工/人工客服」。
- 涉及投诉、退款、合规、隐私、定价谈判等敏感事务。
- **检索 2~3 次都没有相关知识** —— 这是一个私域客服 agent，知识库覆盖之外的问题**不能**用通用知识硬答，应当转人工，避免给出错误或臆造的产品信息。

【关于多条回复】
- 客户可能一次连发了好几条消息（都还没回过）。综合所有未回消息**通盘考虑**，再决定回复方式。
- 带有「历史」标签的消息只是背景，不能算作客户本轮重复发送；判断“再次/连续/第几次询问”时，只看最后的「本轮未回复消息」。
- 默认 `final_reply` 用单条 `text` 把所有问题一并答复，更省屏占且不打扰。
- 仅当客户**问了多个明显独立的问题**、或合并后过长不便阅读时，才改用 `replies: [...]` 拆成几条短气泡（≤ 6 条）。
- 也允许只回复部分问题（比如其它问题需要等人工补充资料），剩下的用 `escalate_to_human` 标注。

【其它】
- 用 `set_profile_field` 记录客户透露的稳定偏好（行业/角色/预算 等）。
- 准备好正式回复客户时调用 `final_reply`。
- 最多 {max_steps} 步，高效推理，避免冗余工具调用。
"""


async def run_conv_agent(
    *,
    db,
    team_id: int,
    conversation_id: int,
    contact_id: int,
    inbound_text: str,
    trace_id: str,
    system_prompt: str,
    profile_summary: str,
    history,  # list[Message]
    unreplied_chain: list[str] | None = None,
    provider,
    fallback_provider=None,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    max_steps: int = 5,
) -> AgentReplyResult:
    started = time.monotonic()
    registry = get_registry()
    catalog = registry.render_catalog()
    log.info(
        "[agent] enter team=%s conv=%s trace=%s tools=%d text=%r",
        team_id, conversation_id, trace_id, len(registry.all()), inbound_text,
    )
    if not registry.all():
        # No tools registered usually means the lifespan bootstrap didn't run —
        # don't go into a 5-step empty-catalog loop, fall back fast.
        log.warning("[agent] empty tool registry — aborting to fallback")
        return AgentReplyResult(
            text="稍等，我先核实一下再回您。",
            confidence=0.2, model="", latency_ms=0, steps=0,
        )
    ctx = ToolContext(
        db=db,
        team_id=team_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        inbound_text=inbound_text,
        trace_id=trace_id,
    )

    system = _SYSTEM_TEMPLATE.format(
        system=system_prompt.strip() or "（无）",
        profile=profile_summary.strip() or "（暂无）",
        tools=catalog or "（空）",
        max_steps=max_steps,
    )

    base_msgs: list[ChatMessage] = [ChatMessage(role="system", content=system)]
    for m in history:
        role = "user" if m.direction == "in" else "assistant"
        label = "历史客户消息（已处理，仅供背景）" if role == "user" else "历史客服回复（已发送，仅供背景）"
        base_msgs.append(ChatMessage(role=role, content=f"【{label}】{m.content}"))

    # If the customer fired multiple unreplied messages, surface them as a
    # single packaged user turn so the agent can reason over the full intent.
    chain = [c for c in (unreplied_chain or []) if c]
    if not chain:
        chain = [inbound_text]
    if len(chain) > 1:
        bundle = "【本轮未回复消息，共 {} 条，按时间从早到晚】\n".format(len(chain))
        for i, c in enumerate(chain, 1):
            bundle += f"\n[{i}] {c}"
        bundle += "\n\n请只基于本轮未回复消息判断客户当前是否连续发送，并决定用一条回复合并应对，或者用多条 replies（适合多个独立问题）。"
        base_msgs.append(ChatMessage(role="user", content=bundle))
    else:
        base_msgs.append(ChatMessage(role="user", content=f"【本轮未回复消息，共 1 条】\n[1] {chain[0]}"))

    trace: list[dict[str, Any]] = []
    scratch_for_step: list[ChatMessage] = []
    last_model = ""
    final_text: str | None = None
    final_confidence = 0.0

    for step in range(1, max_steps + 1):
        msgs = base_msgs + scratch_for_step
        t_llm = time.monotonic()
        # Cap temperature for the reasoning loop. The user-configured value is
        # tuned for the final reply tone, but high temperature here breaks the
        # strict-JSON / tool-naming contract.
        agent_temp = min(float(temperature), 0.25)
        try:
            result = await asyncio.wait_for(
                provider.chat(msgs, temperature=agent_temp, max_tokens=max_tokens),
                timeout=_STEP_LLM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[agent] step %d LLM timeout after %ds", step, int(_STEP_LLM_TIMEOUT_SEC),
            )
            trace.append({"step": step, "error": f"llm_timeout_{int(_STEP_LLM_TIMEOUT_SEC)}s"})
            break
        except Exception as e:  # noqa: BLE001
            if fallback_provider is None:
                log.exception("agent LLM call failed")
                trace.append({"step": step, "error": f"llm_error: {e}"})
                break
            log.warning("[agent] primary LLM failed, trying fallback: %s", e)
            try:
                result = await asyncio.wait_for(
                    fallback_provider.chat(msgs, temperature=agent_temp, max_tokens=max_tokens),
                    timeout=_STEP_LLM_TIMEOUT_SEC,
                )
            except Exception as fallback_e:  # noqa: BLE001
                log.exception("agent fallback LLM call failed")
                trace.append({"step": step, "error": f"llm_error: {e}; fallback_error: {fallback_e}"})
                break
        log.info(
            "[agent] step %d LLM ok (%dms, %d tokens out approx)",
            step, int((time.monotonic() - t_llm) * 1000), len(result.text or "") // 4,
        )
        last_model = result.model
        decision = _parse_json(result.text)
        if decision.get("_parse_error"):
            if fallback_provider is not None and provider is not fallback_provider:
                try:
                    fallback_result = await asyncio.wait_for(
                        fallback_provider.chat(msgs, temperature=agent_temp, max_tokens=max_tokens),
                        timeout=_STEP_LLM_TIMEOUT_SEC,
                    )
                    fallback_decision = _parse_json(fallback_result.text)
                    if not fallback_decision.get("_parse_error"):
                        log.info("[agent] fallback model repaired bad_json at step %d", step)
                        result = fallback_result
                        decision = fallback_decision
                    else:
                        log.warning("[agent] fallback also bad_json raw=%r", fallback_result.text)
                except Exception as e:  # noqa: BLE001
                    log.warning("[agent] fallback retry after bad_json failed: %s", e)
            if not decision.get("_parse_error"):
                pass
            else:
                raw = str(decision.get("raw") or "").strip()
                log.warning("[agent] step %d bad_json raw=%r", step, raw)
                trace.append({"step": step, "error": "bad_json", "raw": raw})
                if ctx.scratch.get("kb_hit_ids") and len(raw) >= 20 and not raw.lstrip().startswith("{"):
                    final_text = raw
                    final_confidence = 0.7
                    break
                scratch_for_step.append(
                    ChatMessage(role="assistant", content=raw or "（空响应）")
                )
                scratch_for_step.append(
                    ChatMessage(
                        role="user",
                        content=(
                            "[observation]\nERROR: 你的上一条回复不是工具 JSON。"
                            "请严格输出 JSON，并调用 final_reply 或其它可用工具。"
                        ),
                    )
                )
                continue
        tool_name = (decision.get("tool") or "").strip()
        thought = (decision.get("thought") or "").strip()
        args = decision.get("args") or {}
        log.info(
            "[agent] step %d/%d tool=%s thought=%s args=%s",
            step, max_steps, tool_name, _render_log_value(thought), _render_log_value(args),
        )
        trace.append({"step": step, "tool": tool_name, "thought": thought, "args": args})

        tool = registry.get(tool_name)
        if tool is None:
            obs = f"ERROR: 未知工具 {tool_name!r}。请改用工具表里列出的名字。"
            scratch_for_step.append(
                ChatMessage(role="assistant", content=json.dumps(decision, ensure_ascii=False))
            )
            scratch_for_step.append(
                ChatMessage(role="user", content=f"[observation]\n{obs}")
            )
            trace[-1]["observation"] = obs
            continue

        try:
            obs = await asyncio.wait_for(tool.call(ctx, args), timeout=_TOOL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            obs = f"ERROR: tool {tool_name} 超过 {int(_TOOL_TIMEOUT_SEC)}s 未返回"
            log.warning("[agent] tool %s timeout", tool_name)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s raised", tool_name)
            obs = f"ERROR: tool 抛错: {e}"
        log.info("[agent] step %d obs=%s", step, obs)
        trace[-1]["observation"] = obs

        # Terminal tools short-circuit the loop.
        if tool_name == "final_reply" and (
            "final_text" in ctx.scratch or "final_replies" in ctx.scratch
        ):
            final_text = ctx.scratch.get("final_text")
            final_confidence = float(ctx.scratch.get("final_confidence", 0.7))
            break
        if tool_name == "escalate_to_human":
            break

        # Otherwise feed the LLM the action it picked + the observation, so it
        # can continue reasoning.
        scratch_for_step.append(
            ChatMessage(role="assistant", content=json.dumps(decision, ensure_ascii=False))
        )
        scratch_for_step.append(
            ChatMessage(role="user", content=f"[observation]\n{obs}")
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    escalate = bool(ctx.scratch.get("escalate"))

    # If the LLM never reached final_reply (timeout / wandering), fall back to
    # a polite stall — better than dropping the customer on the floor.
    if final_text is None and not escalate:
        final_text = "稍等一下，我帮您再核实一下。"
        final_confidence = 0.3
        log.warning("[agent] loop ended without final_reply; fallback used")

    return AgentReplyResult(
        text=final_text,
        replies=list(ctx.scratch.get("final_replies") or []),
        confidence=final_confidence,
        model=last_model,
        latency_ms=elapsed_ms,
        steps=len(trace),
        escalate=escalate,
        escalate_reason=str(ctx.scratch.get("escalate_reason", "")),
        kb_hit_ids=list(ctx.scratch.get("kb_hit_ids") or []),
        kb_context=str(ctx.scratch.get("kb_context") or ""),
        trace=trace,
    )


# ---- helpers --------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(s: str) -> dict[str, Any]:
    s = (s or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = _JSON_BLOCK.search(s)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            pass
    return {
        "thought": "LLM 输出无法解析为 JSON",
        "tool": "__parse_error__",
        "args": {},
        "_parse_error": True,
        "raw": s,
    }


def _render_log_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
