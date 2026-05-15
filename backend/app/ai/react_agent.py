"""ReAct device agent — goal-oriented automation of the WeCom Android client.

Architecture:
  - Caller passes a natural-language `goal` (e.g. "open chat with 七月 and send
    'hello'") plus a robot reference.
  - Each iteration:
      1. Pull UI tree + screenshot from device.
      2. Number every node; format tree text with `[N]` prefixes.
      3. Send tree (+ optional screenshot for vision models) to LLM with a
         strict JSON tool-use protocol.
      4. LLM picks an action by *node id*, not coordinates. The backend
         resolves the id to bounds and dispatches the right primitive to the
         device.
  - Loop until `done(success/fail)` or `max_steps`.

The LLM never sees raw screen pixels of node positions — it picks nodes from
a numbered list. The backend computes the centre of the node's bounds and
sends `tap_xy` / `input_text` / `swipe` to the device. This keeps the agent's
reasoning anchored to the UI tree (which we can audit) while still letting
vision models compensate for icon-only buttons by looking at the screenshot.
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
from app.core.config import settings
from app.device import DeviceClient, UiNode
from app.models import Robot
from app.services import settings_service

log = logging.getLogger(__name__)


# ---- tool catalogue --------------------------------------------------------
# Action names + descriptions exposed to the LLM. The LLM picks nodes by id
# from the numbered tree; coordinates are never the LLM's concern.
TOOL_SCHEMA = {
    "tap_node": {
        "desc": "点击编号为 node_id 的 UI 节点（看 UI tree 里的 [N] 前缀）。优先用本工具。",
        "args": {"node_id": "int — UI tree 中节点的编号"},
    },
    "input_text": {
        "desc": "在编号为 node_id 的可编辑节点里输入文本。",
        "args": {"node_id": "int — 必须是可编辑的输入框节点", "text": "string"},
    },
    "swipe": {
        "desc": "滑屏。方向四选一：up / down / left / right。一般用于滚动列表。",
        "args": {"direction": "up|down|left|right", "node_id": "可选 int — 在某节点内滑（默认全屏）"},
    },
    "back": {"desc": "系统返回键。", "args": {}},
    "home": {"desc": "回主屏。", "args": {}},
    "open_wecom": {
        "desc": "把企业微信 (com.tencent.wework) 切到前台。如果你看到的不是 WeCom 应用，**先调用本工具**。",
        "args": {},
    },
    "done": {
        "desc": "认为目标已达成或确认无法完成时调用，结束本轮。",
        "args": {"success": "bool", "summary": "string — 给运营看的一句话总结"},
    },
}


# ---- types ----------------------------------------------------------------
@dataclass
class _Observation:
    tree: str
    nodes: dict[int, UiNode]
    screen_size: tuple[int, int]
    screenshot_b64: str | None = None  # JPEG base64 if available
    screenshot_mime: str = "image/jpeg"


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


# ---- top-level loop -------------------------------------------------------
async def run_react(
    db: AsyncSession,
    robot: Robot,
    goal: str,
    *,
    max_steps: int = 6,
    step_timeout: float = 12.0,
    log_sink: LogSink = None,
) -> AgentResult:
    started = time.monotonic()
    steps: list[AgentStep] = []

    async def _log(level: str, msg: str) -> None:
        getattr(log, level if level != "warn" else "warning")(msg)
        if log_sink is not None:
            try:
                await log_sink(level, msg)
            except Exception:  # noqa: BLE001
                pass

    await _log("info", f"[react] goal={goal!r} max_steps={max_steps}")
    provider = await get_provider(db, robot.team_id)
    llm_cfg = await settings_service.get(db, robot.team_id, "llm")
    use_vision = _vision_enabled(llm_cfg)

    for i in range(1, max_steps + 1):
        # ---- observe ----
        try:
            obs = await _observe(robot, want_screenshot=use_vision)
        except TimeoutError as e:
            await _log("error", f"[react] step {i} observe timeout: {e}")
            return AgentResult(ok=False, summary=f"observe 超时：{e}", steps=steps)
        except Exception as e:  # noqa: BLE001
            await _log("error", f"[react] step {i} observe failed: {e}")
            return AgentResult(ok=False, summary=f"observe 失败：{e}", steps=steps)

        # ---- decide ----
        try:
            decision = await _decide(provider, goal, obs, steps, use_vision=use_vision)
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
            return AgentResult(ok=False, summary=f"未知动作 {action}", steps=steps)

        # ---- act (resolve node_id → device primitive) ----
        t0 = time.monotonic()
        try:
            ok, msg = await _execute(robot, action, args, obs, step_timeout=step_timeout)
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
        await asyncio.sleep(0.4)

    total = int((time.monotonic() - started) * 1000)
    await _log("warn", f"[react] hit max_steps={max_steps} after {total}ms")
    return AgentResult(ok=False, summary=f"达到最大步数 {max_steps}，未完成目标", steps=steps)


# ---- observation ---------------------------------------------------------
async def _observe(robot: Robot, *, want_screenshot: bool) -> _Observation:
    device = DeviceClient(robot)
    dump = await device.dump_ui(reason="react", timeout=8.0)
    nodes = {n.id: n for n in dump.nodes if len(n.bounds) == 4}
    screen_w = int(dump.screen_width or 0)
    screen_h = int(dump.screen_height or 0)

    screenshot_b64: str | None = None
    screenshot_mime = "image/jpeg"
    if want_screenshot:
        try:
            shot = await device.screenshot_once(timeout=10.0)
            data = shot.data or {}
            screenshot_b64 = data.get("image")
            if data:
                screenshot_mime = str(data.get("mime") or "image/jpeg")
        except Exception:  # noqa: BLE001
            log.debug("[react] screenshot fetch failed; falling back to tree-only")

    return _Observation(
        tree=_shrink_tree(dump.tree),
        nodes=nodes,
        screen_size=(screen_w, screen_h),
        screenshot_b64=screenshot_b64,
        screenshot_mime=screenshot_mime,
    )


_MAX_TREE_CHARS = 4500


def _shrink_tree(tree: str) -> str:
    """Keep only informative lines: numbered nodes that have text/desc/id or
    are clickable/editable. Pure structural FrameLayouts are dropped."""
    keep: list[str] = []
    for line in tree.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("==="):
            keep.append(line)
            continue
        # Numbered lines are `[N] [Class] ...` — we want the ones with content.
        if "txt=" in line or "desc=" in line or " id=" in line:
            keep.append(line)
            continue
        # Even content-less nodes are kept if they look clickable/editable —
        # the LLM might need them (e.g. an icon-only search button).
        if re.search(r"\b[CFES]+$", s):
            keep.append(line)
    out = "\n".join(keep)
    if len(out) > _MAX_TREE_CHARS:
        out = out[:_MAX_TREE_CHARS] + "\n…(truncated)"
    return out


# ---- decide --------------------------------------------------------------
def _vision_enabled(llm_cfg: dict) -> bool:
    """Vision is on when either env default or per-team override says so."""
    cfg_val = llm_cfg.get("supports_vision")
    if cfg_val is None:
        return bool(settings.llm_supports_vision)
    return bool(cfg_val)


def _tools_block() -> str:
    parts = []
    for name, meta in TOOL_SCHEMA.items():
        args = ", ".join(f"{k}: {v}" for k, v in meta["args"].items()) or "无"
        parts.append(f"- {name}({args}) — {meta['desc']}")
    return "\n".join(parts)


_SYSTEM_PROMPT = """你是一名移动端 UI 操作专家。给定一个目标和当前屏幕的可访问性树（UI tree），按节点编号选出下一步动作。

可用工具：
{tools}

返回严格 JSON（**不要 Markdown 代码块、不要多余文字**）：
{{
  "thought": "中文思考",
  "action": "工具名",
  "args": {{ ... }}
}}

规则：
1. 只操作 UI tree 里已经列出的节点。优先用 tap_node(node_id) 而不是猜测坐标。
2. 节点没有可见文字（例如只是个 ImageView 图标）时，结合截图判断它的语义。
3. 如果当前 root 包名不是 com.tencent.wework，第一步必须用 open_wecom。
4. 找不到目标节点时，先 swipe 滚动；连续 2~3 步无进展则用 done(success=false) 退出，不要硬猜。
5. 一次只输出一个动作。"""


async def _decide(
    provider,
    goal: str,
    obs: _Observation,
    history: list[AgentStep],
    *,
    use_vision: bool,
) -> dict[str, Any]:
    sys = _SYSTEM_PROMPT.format(tools=_tools_block())
    hist_lines = []
    for s in history[-5:]:
        hist_lines.append(
            f"#{s.index} action={s.action} args={_short(s.args)} ok={s.ok} msg={s.message!r}"
        )
    hist = "\n".join(hist_lines) if hist_lines else "（无）"
    user_text = (
        f"【目标】{goal}\n\n"
        f"【最近的执行历史】\n{hist}\n\n"
        f"【屏幕尺寸】{obs.screen_size[0]} x {obs.screen_size[1]}\n"
        f"【UI tree（节点已编号）】\n{obs.tree}\n"
    )

    images: list[tuple[str, str]] = []
    if use_vision and obs.screenshot_b64:
        images.append((obs.screenshot_mime, obs.screenshot_b64))

    msgs = [
        ChatMessage(role="system", content=sys),
        ChatMessage(role="user", content=user_text, images=images),
    ]
    result = await provider.chat(msgs, temperature=0.1, max_tokens=8192)
    text = (result.text or "").strip()
    if not text:
        log.warning("[react] LLM returned empty body; model=%s", result.model)
    parsed = _parse_json(text)
    if parsed.get("action") == "done" and parsed.get("args", {}).get("summary", "").startswith("bad_json"):
        log.warning("[react] bad_json raw=%r model=%s", text[:400], result.model)
    return parsed


# ---- execute (resolve node → device primitive) --------------------------
async def _execute(
    robot: Robot,
    action: str,
    args: dict[str, Any],
    obs: _Observation,
    *,
    step_timeout: float,
) -> tuple[bool, str]:
    device = DeviceClient(robot)
    if action == "tap_node":
        node = _lookup_node(obs, args.get("node_id"))
        if node is None:
            return False, f"node_id={args.get('node_id')} 不在 UI tree 中"
        cx, cy = node.center()
        # Prefer accessibility ACTION_CLICK via tap_text if the node has a
        # distinctive text — more robust to small layout shifts. Otherwise
        # fall back to coordinate tap.
        if node.text and len(node.text) >= 2:
            ack = await device.tap_text(node.text, timeout=step_timeout)
            if ack.ok:
                return True, f"tap_text({node.text!r}) -> {ack.message or 'ok'}"
        ack = await device.tap_xy(cx, cy, timeout=step_timeout)
        return ack.ok, f"tap_xy({cx},{cy}) -> {ack.message or ''}"

    if action == "input_text":
        node_id = args.get("node_id")
        text = args.get("text") or ""
        if node_id is not None:
            node = _lookup_node(obs, node_id)
            if node is None:
                return False, f"node_id={node_id} 不在 UI tree 中"
            if not node.editable:
                return False, f"node {node_id} 不可编辑（cls={node.cls}）"
            # Focus the input first (tap), then write text.
            cx, cy = node.center()
            await device.tap_xy(cx, cy, timeout=step_timeout)
            await asyncio.sleep(0.25)
        ack = await device.input_text(text, timeout=step_timeout)
        return ack.ok, ack.message or ""

    if action == "swipe":
        direction = (args.get("direction") or "up").lower()
        target_node = _lookup_node(obs, args.get("node_id"))
        x1, y1, x2, y2 = _swipe_coords(direction, obs, target_node)
        ack = await device.swipe(x1, y1, x2, y2, duration_ms=280, timeout=step_timeout)
        return ack.ok, ack.message or ""

    if action == "back":
        ack = await device.back(timeout=step_timeout)
        return ack.ok, ack.message or ""

    if action == "home":
        ack = await device.home(timeout=step_timeout)
        return ack.ok, ack.message or ""

    if action == "open_wecom":
        ack = await device.open_wecom(timeout=step_timeout)
        return ack.ok, ack.message or ""

    return False, f"unhandled action: {action}"


def _lookup_node(obs: _Observation, node_id: Any) -> UiNode | None:
    try:
        nid = int(node_id)
    except (TypeError, ValueError):
        return None
    return obs.nodes.get(nid)


def _swipe_coords(
    direction: str, obs: _Observation, target: UiNode | None
) -> tuple[int, int, int, int]:
    """Pick reasonable swipe endpoints. If `target` is given we swipe inside
    its bounds (e.g. scrolling a specific list), otherwise the full screen."""
    if target is not None:
        l, t, r, b = target.bounds
    else:
        w, h = obs.screen_size
        if w == 0 or h == 0:
            w, h = 1080, 2200
        l, t, r, b = 0, int(h * 0.15), w, int(h * 0.85)
    cx = (l + r) // 2
    cy = (t + b) // 2
    dx = (r - l) // 3
    dy = (b - t) // 3
    if direction == "up":
        return cx, cy + dy, cx, cy - dy
    if direction == "down":
        return cx, cy - dy, cx, cy + dy
    if direction == "left":
        return cx + dx, cy, cx - dx, cy
    if direction == "right":
        return cx - dx, cy, cx + dx, cy
    # default: up
    return cx, cy + dy, cx, cy - dy


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
        "thought": "LLM 返回无法解析为 JSON",
        "action": "done",
        "args": {"success": False, "summary": f"bad_json: {s[:120]}"},
    }


def _short(v: Any, n: int = 200) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= n else s[:n] + "…"
