"""ReAct-style fallback agent for Android UI automation.

When the deterministic locators in `WeComAutomator` fail, this agent takes
over: it observes the current UI tree, asks the LLM what to do next, executes
one primitive (tap / swipe / type / back), then loops up to `max_steps`.

Design:
  - Strictly bounded: every run honours `max_steps` (default 6). Logs every
    iteration with thought / action / result for offline post-mortems.
  - JSON-only protocol with the LLM — no function-calling magic, works with
    any OpenAI-compatible backend.
  - Tools map 1:1 to the Android primitives in `AgentForegroundService`.
  - Each step is persisted to the task log if `task_id` is supplied, so the
    web UI's task drawer can replay the whole trajectory.

Returns an `AgentResult` describing the outcome and step count.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import ChatMessage, get_provider
from app.core.ws_manager import hub
from app.models import Robot

log = logging.getLogger(__name__)


# ---- tool catalogue --------------------------------------------------------
# Kept here (not in a schema lib) so it's trivially editable. The descriptions
# are what the LLM sees — keep them tight and unambiguous.
TOOL_SCHEMA = {
    "tap_text": {
        "desc": "点击包含给定文本的节点（会自动向上找可点击容器）。",
        "args": {"text": "string，要匹配的可见文本或 contentDescription"},
    },
    "tap_xy": {
        "desc": "在屏幕坐标 (x, y) 处点击。仅当 tap_text 找不到时使用。",
        "args": {"x": "int 像素", "y": "int 像素"},
    },
    "swipe": {
        "desc": "从 (x1,y1) 滑到 (x2,y2)，常用于上下滚动列表。",
        "args": {"x1": "int", "y1": "int", "x2": "int", "y2": "int",
                  "duration_ms": "int 可选，默认 300"},
    },
    "input_text": {
        "desc": "把文本写入当前聚焦的输入框（必须先 tap 到输入框上）。",
        "args": {"text": "string"},
    },
    "back": {"desc": "执行系统返回（等同 BACK 键）。", "args": {}},
    "home": {"desc": "回到主屏。", "args": {}},
    "done": {
        "desc": "认为目标已经达成，结束本次会话。",
        "args": {"success": "bool", "summary": "string，给运营看的一句话总结"},
    },
}


@dataclass
class AgentStep:
    index: int
    thought: str
    action: str
    args: dict[str, Any]
    ok: bool
    message: str
    elapsed_ms: int


@dataclass
class AgentResult:
    ok: bool
    summary: str
    steps: list[AgentStep] = field(default_factory=list)


LogSink = Callable[[str, str], Awaitable[None]] | None
# (level, message) -> awaitable. Level ∈ {"info","warn","error"}.


async def run_react(
    db: AsyncSession,
    robot: Robot,
    goal: str,
    *,
    max_steps: int = 6,
    step_timeout: float = 12.0,
    log_sink: LogSink = None,
) -> AgentResult:
    """Top-level entry. Caller passes the high-level goal in natural Chinese.

    `log_sink(level, msg)` is invoked for each iteration so the foreground
    service / task log table sees the trajectory in near-real-time. Errors are
    caught and returned as a failed `AgentResult` rather than re-raised — the
    caller should always get a clean dataclass back.
    """
    started = time.monotonic()
    steps: list[AgentStep] = []

    async def _log(level: str, msg: str) -> None:
        # Mirror to logger and (best-effort) the supplied sink.
        getattr(log, level if level != "warn" else "warning")(msg)
        if log_sink is not None:
            try:
                await log_sink(level, msg)
            except Exception:  # noqa: BLE001
                pass

    await _log("info", f"[react] goal={goal!r} max_steps={max_steps}")
    provider = await get_provider(db, robot.team_id)

    for i in range(1, max_steps + 1):
        # ---- observe ----
        try:
            observation = await _observe(robot)
        except TimeoutError as e:
            await _log("error", f"[react] step {i} observe timeout: {e}")
            return AgentResult(
                ok=False, summary=f"observe 超时：{e}", steps=steps
            )
        except Exception as e:  # noqa: BLE001
            await _log("error", f"[react] step {i} observe failed: {e}")
            return AgentResult(ok=False, summary=f"observe 失败：{e}", steps=steps)

        # ---- decide ----
        try:
            decision = await _decide(provider, goal, observation, steps)
        except Exception as e:  # noqa: BLE001
            await _log("error", f"[react] step {i} llm failed: {e}")
            return AgentResult(ok=False, summary=f"LLM 调用失败：{e}", steps=steps)

        thought = decision.get("thought") or ""
        action = (decision.get("action") or "").strip()
        args = decision.get("args") or {}
        await _log(
            "info",
            f"[react] step {i}/{max_steps} thought={thought!r} action={action} args={_short(args)}",
        )

        if action == "done":
            success = bool(args.get("success", True))
            summary = str(args.get("summary") or "")
            steps.append(AgentStep(i, thought, action, args, success, summary, 0))
            return AgentResult(ok=success, summary=summary or "agent done", steps=steps)

        if action not in TOOL_SCHEMA:
            await _log("warn", f"[react] unknown action {action!r}, aborting")
            return AgentResult(
                ok=False, summary=f"未知动作 {action}", steps=steps
            )

        # ---- act ----
        t0 = time.monotonic()
        try:
            ack = await asyncio.wait_for(
                hub.send_request(robot.robot_id, "device.command", {"command": action, **args}),
                timeout=step_timeout,
            )
            ok = bool(ack.get("ok"))
            msg = str(ack.get("message") or "")
        except TimeoutError as e:
            ok, msg = False, f"timeout: {e}"
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"exec error: {e}"
        elapsed = int((time.monotonic() - t0) * 1000)
        await _log(
            "info" if ok else "warn",
            f"[react] step {i} → ok={ok} msg={msg!r} ({elapsed}ms)",
        )
        steps.append(AgentStep(i, thought, action, args, ok, msg, elapsed))

        # Small breather so the UI can settle before the next observation.
        await asyncio.sleep(0.4)

    total = int((time.monotonic() - started) * 1000)
    await _log("warn", f"[react] hit max_steps={max_steps} after {total}ms")
    return AgentResult(
        ok=False, summary=f"达到最大步数 {max_steps}，未完成目标", steps=steps
    )


# ---------------------------------------------------------------- observe ---
async def _observe(robot: Robot) -> dict[str, Any]:
    """Pull the current UI tree from the device. We use the existing dump_ui
    pathway so we get the same `device.ui_dump` event the manual button uses.
    """
    res = await hub.send_request(
        robot.robot_id,
        "device.command",
        {"command": "dump_ui", "reason": "react"},
        timeout=8.0,
    )
    return {
        "current_page": res.get("current_page"),
        "tree": _shrink_tree(res.get("tree") or ""),
    }


_MAX_TREE_CHARS = 3500


def _shrink_tree(tree: str) -> str:
    """Drop empty / decorative nodes to keep the prompt small. We keep lines
    that have text/desc/id or are clickable — the rest is structural noise."""
    keep: list[str] = []
    for line in tree.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("==="):
            keep.append(line)
            continue
        if "txt=" in line or "desc=" in line or " id=" in line or " C" in line or " E" in line:
            keep.append(line)
    out = "\n".join(keep)
    if len(out) > _MAX_TREE_CHARS:
        out = out[:_MAX_TREE_CHARS] + "\n…(truncated)"
    return out


# ----------------------------------------------------------------- decide ---
def _tools_block() -> str:
    parts = []
    for name, meta in TOOL_SCHEMA.items():
        args = ", ".join(f"{k}: {v}" for k, v in meta["args"].items()) or "无"
        parts.append(f"- {name}({args}) — {meta['desc']}")
    return "\n".join(parts)


_SYSTEM_PROMPT = """你是一名移动端 UI 操作专家。给定一个目标和当前屏幕的可访问性树（UI tree），你要选出**下一步要执行的单个动作**让我们更接近目标。

可用工具：
{tools}

返回严格 JSON，**不要任何额外文字**，结构如下：
{{
  "thought": "用一句中文说明你为什么这么做",
  "action": "上面工具表里的某一个名字",
  "args": {{ ...对应该 action 的参数... }}
}}

注意：
1. 不要凭空臆想节点，先在 UI tree 里找；找不到再考虑切换 tab / 返回 / 搜索。
2. 已完成目标或确认无法完成时使用 done 并给出 success/summary。
3. 一次只输出一个动作。"""


def _user_prompt(goal: str, obs: dict[str, Any], history: list[AgentStep]) -> str:
    hist_lines = []
    for s in history[-5:]:  # most recent 5 only; older context rots
        hist_lines.append(
            f"#{s.index} action={s.action} args={_short(s.args)} ok={s.ok} msg={s.message!r}"
        )
    hist = "\n".join(hist_lines) if hist_lines else "（无）"
    return (
        f"【目标】{goal}\n\n"
        f"【最近的执行历史】\n{hist}\n\n"
        f"【当前页面 hint】{obs.get('current_page')}\n"
        f"【UI tree】\n{obs.get('tree')}\n"
    )


async def _decide(
    provider,
    goal: str,
    obs: dict[str, Any],
    history: list[AgentStep],
) -> dict[str, Any]:
    sys = _SYSTEM_PROMPT.format(tools=_tools_block())
    msgs = [
        ChatMessage(role="system", content=sys),
        ChatMessage(role="user", content=_user_prompt(goal, obs, history)),
    ]
    result = await provider.chat(msgs, temperature=0.1, max_tokens=8192)
    text = (result.text or "").strip()
    if not text:
        log.warning("[react] LLM returned empty body; model=%s latency=%dms", result.model, result.latency_ms)
    parsed = _parse_json(text)
    if parsed.get("action") == "done" and parsed.get("args", {}).get("summary", "").startswith("bad_json"):
        # Log the raw text so we can see what gemma/qwen/etc actually emitted.
        log.warning("[react] bad_json raw=%r model=%s", text[:400], result.model)
    return parsed


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(s: str) -> dict[str, Any]:
    """Tolerant JSON parser — strips Markdown fences and grabs the first
    `{...}` block. Returns a `done(success=false)` shaped dict on parse fail
    so the loop terminates cleanly rather than crashing."""
    s = s.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
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
        "thought": "LLM 返回无法解析为 JSON",
        "action": "done",
        "args": {"success": False, "summary": f"bad_json: {s[:120]}"},
    }


def _short(d: dict[str, Any]) -> str:
    """Compact dict repr for log lines."""
    if not d:
        return "{}"
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 40:
            out[k] = v[:40] + "…"
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)
