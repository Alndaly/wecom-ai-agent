"""ReAct device agent — goal-oriented automation of the WeCom Android client.

Architecture:
  - Caller passes a natural-language `goal` (e.g. "open chat with 七月 and send
    'hello'") plus a robot reference.
  - Each iteration:
      1. Pull UI tree from device.
      2. Try a deterministic fast path for common send-message flows.
      3. If no reasonable node is found, attach an optional screenshot and ask
         the LLM for a strict JSON tool-use decision.
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

from app.ai.providers import ChatMessage, get_fallback_provider, get_provider
from app.ai.react_locators import LocatorStore, parse_send_goal, role_for_decision
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
    force_llm: bool = False,
) -> AgentResult:
    """Drive the device toward `goal` step-by-step.

    `force_llm=True` bypasses the deterministic fast path and asks the LLM at
    every iteration (with UI tree + optional screenshot). Useful when you want
    the agent to never short-circuit to rules — e.g. testing the LLM's UI
    judgment on a tricky flow. AI still picks **node ids**, not coordinates;
    the backend resolves bounds → tap_xy / input_text.
    """
    started = time.monotonic()
    steps: list[AgentStep] = []

    async def _log(level: str, msg: str) -> None:
        getattr(log, level if level != "warn" else "warning")(msg)
        if log_sink is not None:
            try:
                await log_sink(level, msg)
            except Exception:  # noqa: BLE001
                pass

    await _log(
        "info",
        f"[react] goal={goal!r} max_steps={max_steps} mode={'llm_only' if force_llm else 'rule+llm'}",
    )
    provider = await get_provider(db, robot.team_id)
    fallback_provider = await get_fallback_provider(db, robot.team_id)
    llm_cfg = await settings_service.get(db, robot.team_id, "llm")
    use_vision = _vision_enabled(llm_cfg)
    locator_store = LocatorStore(robot)

    for i in range(1, max_steps + 1):
        # ---- observe ----
        try:
            obs = await _observe(robot, want_screenshot=False)
            await _log(
                "info",
                f"[react] step {i} observed nodes={len(obs.nodes)} "
                f"screen={obs.screen_size[0]}x{obs.screen_size[1]} screenshot=no",
            )
        except TimeoutError as e:
            await _log("error", f"[react] step {i} observe timeout: {e}")
            return AgentResult(ok=False, summary=f"observe 超时：{e}", steps=steps)
        except Exception as e:  # noqa: BLE001
            await _log("error", f"[react] step {i} observe failed: {e}")
            return AgentResult(ok=False, summary=f"observe 失败：{e}", steps=steps)

        # ---- decide ----
        if force_llm:
            decision, decision_source = None, "llm"
        else:
            decision, decision_source = _fast_decide(goal, obs, steps, locator_store)
        if decision is None:
            decision_source = "llm"
            if use_vision:
                obs = await _attach_screenshot(robot, obs)
            await _log(
                "info",
                f"[react] step {i} fast path miss; fallback={decision_source} "
                f"screenshot={'yes' if obs.screenshot_b64 else 'no'}",
            )
            try:
                decision = await _decide(provider, goal, obs, steps, use_vision=use_vision)
                if fallback_provider is not None and decision.get("action") == "done" and not decision.get("args", {}).get("success", True):
                    await _log("info", "[react] primary model gave failure; trying fallback model")
                    fallback_decision = await _decide(fallback_provider, goal, obs, steps, use_vision=use_vision)
                    if not (fallback_decision.get("action") == "done" and not fallback_decision.get("args", {}).get("success", True)):
                        decision = fallback_decision
            except Exception as e:  # noqa: BLE001
                if fallback_provider is None:
                    await _log("error", f"[react] step {i} llm failed: {e}")
                    return AgentResult(ok=False, summary=f"LLM 调用失败：{e}", steps=steps)
                await _log("warn", f"[react] primary llm failed, trying fallback: {e}")
                try:
                    decision = await _decide(fallback_provider, goal, obs, steps, use_vision=use_vision)
                except Exception as fallback_e:  # noqa: BLE001
                    await _log("error", f"[react] fallback llm failed: {fallback_e}")
                    return AgentResult(ok=False, summary=f"LLM 调用失败：{e}; fallback={fallback_e}", steps=steps)

        thought = decision.get("thought") or ""
        action = (decision.get("action") or "").strip()
        args = decision.get("args") or {}
        await _log(
            "info",
            f"[react] step {i}/{max_steps} source={decision_source} "
            f"thought={thought!r} action={action} args={_short(args)}",
        )

        if action == "done":
            success = bool(args.get("success", True))
            summary = str(args.get("summary") or "")
            if decision_source == "llm":
                artifact = locator_store.save_fallback_artifact(
                    goal=goal,
                    step_index=i,
                    obs_tree=obs.tree,
                    nodes=obs.nodes,
                    screen_size=obs.screen_size,
                    screenshot_b64=obs.screenshot_b64,
                    screenshot_mime=obs.screenshot_mime,
                    decision=decision,
                    ok=success,
                    message=summary,
                )
                await _log("info", f"[react] fallback artifact={artifact}")
            steps.append(AgentStep(i, thought, action, args, success, summary, 0))
            return AgentResult(ok=success, summary=summary or "agent done", steps=steps)

        if action not in TOOL_SCHEMA:
            if decision_source == "llm":
                artifact = locator_store.save_fallback_artifact(
                    goal=goal,
                    step_index=i,
                    obs_tree=obs.tree,
                    nodes=obs.nodes,
                    screen_size=obs.screen_size,
                    screenshot_b64=obs.screenshot_b64,
                    screenshot_mime=obs.screenshot_mime,
                    decision=decision,
                    ok=False,
                    message=f"未知动作 {action}",
                )
                await _log("info", f"[react] fallback artifact={artifact}")
            await _log("warn", f"[react] unknown action {action!r}, aborting")
            return AgentResult(ok=False, summary=f"未知动作 {action}", steps=steps)

        # ---- act (resolve node_id → device primitive) ----
        t0 = time.monotonic()
        used_node = _lookup_node(obs, args.get("node_id"))
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
        role = str(args.get("_locator_role") or "") or _infer_locator_role(action, args, used_node, goal, obs)
        if decision_source == "llm":
            artifact = locator_store.save_fallback_artifact(
                goal=goal,
                step_index=i,
                obs_tree=obs.tree,
                nodes=obs.nodes,
                screen_size=obs.screen_size,
                screenshot_b64=obs.screenshot_b64,
                screenshot_mime=obs.screenshot_mime,
                decision=decision,
                ok=ok,
                message=msg,
            )
            await _log("info", f"[react] fallback artifact={artifact}")
            if ok and used_node is not None and role:
                parsed_goal = parse_send_goal(goal)
                learn_node = used_node
                if role == "chat_target" and parsed_goal is not None:
                    learn_node = _node_with_label_inside(obs, used_node, parsed_goal.target) or used_node
                elif role == "send_button":
                    learn_node = (
                        _node_with_label_inside(obs, used_node, "发送")
                        or _node_with_label_inside(obs, used_node, "Send")
                        or used_node
                    )
                elif role == "search_entry":
                    learn_node = _search_node_inside(obs, used_node) or used_node
                locator_store.remember_success(
                    role=role,
                    action=action,
                    node=learn_node,
                    obs_meta={"node_count": len(obs.nodes), "screen_size": list(obs.screen_size)},
                    source="llm",
                    target=(parsed_goal.target if parsed_goal else None),
                )
                await _log("info", f"[react] locator learned role={role} node={learn_node.id}")
        elif decision_source == "cache" and not ok:
            locator_store.remember_failure(role=role)
            await _log("warn", f"[react] cached locator failed role={role or 'unknown'}; will fallback if needed")
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


async def _attach_screenshot(robot: Robot, obs: _Observation) -> _Observation:
    device = DeviceClient(robot)
    try:
        shot = await device.screenshot_once(timeout=10.0)
        data = shot.data or {}
        screenshot_b64 = data.get("image")
        screenshot_mime = str(data.get("mime") or "image/jpeg") if data else "image/jpeg"
    except Exception:  # noqa: BLE001
        log.debug("[react] screenshot fetch failed; falling back to tree-only")
        screenshot_b64 = None
        screenshot_mime = "image/jpeg"
    return _Observation(
        tree=obs.tree,
        nodes=obs.nodes,
        screen_size=obs.screen_size,
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


def _fast_decide(
    goal: str, obs: _Observation, history: list[AgentStep], locator_store: LocatorStore
) -> tuple[dict[str, Any] | None, str]:
    """Cached locator path + generic rule bootstrap for send-message flows.

    Cache wins first. The generic rules are only a bootstrap and a recovery
    path; successful LLM fallbacks refresh the cache with current node features.
    """
    parsed = parse_send_goal(goal)
    if parsed is None:
        return None, "none"
    target, text = parsed.target, parsed.text
    failed_cache_roles = {
        str(s.args.get("_locator_role"))
        for s in history
        if not s.ok and s.args.get("_locator_role")
    }

    if not _is_wecom_tree(obs.tree):
        return {
            "thought": "当前不在企业微信，先切到前台。",
            "action": "open_wecom",
            "args": {},
        }, "rule"

    if _last_success(history, "tap_node", locator_role="send_button"):
        return {
            "thought": "上一轮已经点击发送按钮，目标完成。",
            "action": "done",
            "args": {"success": True, "summary": f"已向 {target} 发送消息。"},
        }, "rule"

    if _last_success(history, "input_text"):
        search_input_done = _last_success(history, "input_text", locator_role="search_input")
        if search_input_done:
            cached_target_after_search = None if "chat_target" in failed_cache_roles else locator_store.match("chat_target", obs.nodes, target=target)
            if cached_target_after_search is not None:
                return {
                    "thought": "搜索已输入，命中缓存的搜索结果会话 locator。",
                    "action": "tap_node",
                    "args": {"node_id": cached_target_after_search.id, "_locator_role": "chat_target"},
                }, "cache"
            target_after_search = _find_text_node(obs, target)
            if target_after_search is not None:
                return {
                    "thought": "搜索结果中找到目标联系人，直接打开会话。",
                    "action": "tap_node",
                    "args": {"node_id": target_after_search.id, "_locator_role": "chat_target"},
                }, "rule"
            return None, "none"

        cached_send = None if "send_button" in failed_cache_roles else locator_store.match("send_button", obs.nodes, target=target)
        if cached_send is not None:
            return {
                "thought": "消息已输入，命中缓存的发送按钮 locator。",
                "action": "tap_node",
                "args": {"node_id": cached_send.id, "_locator_role": "send_button"},
            }, "cache"
        send_node = _find_send_button(obs)
        if send_node is not None:
            return {
                "thought": "消息已输入，当前找到发送按钮，直接发送。",
                "action": "tap_node",
                "args": {"node_id": send_node.id, "_locator_role": "send_button"},
            }, "rule"
        return None, "none"

    if _last_success(history, "tap_node", locator_role="chat_target"):
        cached_message_input = None if "message_input" in failed_cache_roles else locator_store.match("message_input", obs.nodes, target=target)
        if cached_message_input is not None:
            return {
                "thought": "目标会话已打开，命中缓存的消息输入框 locator。",
                "action": "input_text",
                "args": {"node_id": cached_message_input.id, "text": text, "_locator_role": "message_input"},
            }, "cache"
        message_input = _find_message_input(obs)
        if message_input is not None:
            return {
                "thought": "目标会话已打开，找到消息输入框，输入目标文本。",
                "action": "input_text",
                "args": {"node_id": message_input.id, "text": text, "_locator_role": "message_input"},
            }, "rule"

    if _last_success(history, "tap_node", locator_role="search_entry"):
        cached_search_input = None if "search_input" in failed_cache_roles else locator_store.match("search_input", obs.nodes, target=target)
        if cached_search_input is not None:
            return {
                "thought": "搜索入口已打开，命中缓存的搜索输入框 locator。",
                "action": "input_text",
                "args": {"node_id": cached_search_input.id, "text": target, "_locator_role": "search_input"},
            }, "cache"
        search_input = _find_search_input(obs)
        if search_input is not None:
            return {
                "thought": "搜索入口已打开，找到搜索输入框，输入目标联系人。",
                "action": "input_text",
                "args": {"node_id": search_input.id, "text": target, "_locator_role": "search_input"},
            }, "rule"
        return None, "none"

    cached_input = None if "message_input" in failed_cache_roles else locator_store.match("message_input", obs.nodes, target=target)
    if cached_input is not None:
        return {
            "thought": "命中缓存的消息输入框 locator，直接输入目标文本。",
            "action": "input_text",
            "args": {"node_id": cached_input.id, "text": text, "_locator_role": "message_input"},
        }, "cache"
    editable = _find_message_input(obs)
    if editable is not None:
        return {
            "thought": "当前已在聊天页并找到消息输入框，直接输入目标文本。",
            "action": "input_text",
            "args": {"node_id": editable.id, "text": text, "_locator_role": "message_input"},
        }, "rule"

    cached_target = None if "chat_target" in failed_cache_roles else locator_store.match("chat_target", obs.nodes, target=target)
    if cached_target is not None:
        return {
            "thought": "命中缓存的会话列表 locator，直接打开目标会话。",
            "action": "tap_node",
            "args": {"node_id": cached_target.id, "_locator_role": "chat_target"},
        }, "cache"
    target_node = _find_text_node(obs, target)
    if target_node is not None:
        return {
            "thought": "聊天列表中找到目标联系人节点，直接打开会话。",
            "action": "tap_node",
            "args": {"node_id": target_node.id, "_locator_role": "chat_target"},
        }, "rule"

    cached_search_entry = None if "search_entry" in failed_cache_roles else locator_store.match("search_entry", obs.nodes, target=target)
    if cached_search_entry is not None:
        return {
            "thought": "首屏未找到目标联系人，命中缓存的搜索入口 locator。",
            "action": "tap_node",
            "args": {"node_id": cached_search_entry.id, "_locator_role": "search_entry"},
        }, "cache"
    search_entry = _find_search_entry(obs)
    if search_entry is not None:
        return {
            "thought": "首屏未找到目标联系人，找到搜索入口，准备搜索。",
            "action": "tap_node",
            "args": {"node_id": search_entry.id, "_locator_role": "search_entry"},
        }, "rule"

    swipe_count = sum(1 for s in history if s.action == "swipe" and s.ok)
    if swipe_count < 1 and _looks_like_list(obs):
        return {
            "thought": "未找到搜索入口且当前像列表，先小幅滚动一次继续查找。",
            "action": "swipe",
            "args": {"direction": "up"},
        }, "rule"
    return None, "none"


def _is_wecom_tree(tree: str) -> bool:
    header = tree.splitlines()[0] if tree else ""
    return "pkg=com.tencent.wework" in header


def _node_label(node: UiNode) -> str:
    return (node.text or node.desc or "").strip()


def _find_text_node(obs: _Observation, text: str) -> UiNode | None:
    candidates = [
        n for n in obs.nodes.values()
        if _node_label(n) == text and len(n.bounds) == 4
    ]
    if not candidates:
        return None
    clickable = [n for n in candidates if n.clickable]
    pool = clickable or candidates
    return min(pool, key=lambda n: (n.bounds[1], n.bounds[0]))


def _find_message_input(obs: _Observation) -> UiNode | None:
    h = obs.screen_size[1] or 2200
    candidates = [
        n for n in obs.nodes.values()
        if n.editable and len(n.bounds) == 4 and n.bounds[1] > h * 0.45
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda n: n.bounds[1])


def _find_search_input(obs: _Observation) -> UiNode | None:
    h = obs.screen_size[1] or 2200
    candidates = [
        n for n in obs.nodes.values()
        if n.editable and len(n.bounds) == 4 and n.bounds[1] < h * 0.45
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda n: (n.bounds[1], n.bounds[0]))


def _find_search_entry(obs: _Observation) -> UiNode | None:
    candidates = []
    for node in obs.nodes.values():
        label = _node_label(node)
        haystack = " ".join([node.view_id, node.cls, node.desc, node.text]).lower()
        if label in {"搜索", "搜一搜", "Search"} or "搜索" in label or "search" in haystack:
            if len(node.bounds) == 4:
                candidates.append(node)
    if not candidates:
        return None
    clickable = [n for n in candidates if n.clickable]
    pool = clickable or candidates
    return min(pool, key=lambda n: (n.bounds[1], n.bounds[0]))


def _find_send_button(obs: _Observation) -> UiNode | None:
    for label in ("发送", "Send"):
        node = _find_text_node(obs, label)
        if node is not None:
            return node
    return None


def _looks_like_list(obs: _Observation) -> bool:
    return any(n.scrollable for n in obs.nodes.values()) or "RecyclerView" in obs.tree


def _last_success(
    history: list[AgentStep],
    action: str,
    *,
    contains_message: str | None = None,
    locator_role: str | None = None,
) -> bool:
    for step in reversed(history):
        if step.action != action:
            continue
        if not step.ok:
            return False
        if contains_message and contains_message not in step.message:
            continue
        if locator_role and step.args.get("_locator_role") != locator_role:
            continue
        return True
    return False


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
5. 一次只输出一个动作。
6. 如果动作对应可复用 UI 位置，请在 args 里额外写 `_locator_role`，取值只能是：
   - chat_target：目标联系人/搜索结果会话
   - search_entry：搜索入口/搜索图标/搜索框占位入口
   - search_input：搜索页输入框
   - message_input：聊天页消息输入框
   - send_button：发送按钮。"""


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
        cx, cy = node.center
        ack = await device.tap_xy(cx, cy, timeout=step_timeout)
        label = _node_label(node)
        label_part = f" label={label!r}" if label else ""
        return ack.ok, f"tap_node({node.id}{label_part}) xy=({cx},{cy}) -> {ack.message or ''}"

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
            cx, cy = node.center
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


def _infer_locator_role(
    action: str,
    args: dict[str, Any],
    node: UiNode | None,
    goal: str,
    obs: _Observation,
) -> str | None:
    direct = role_for_decision(action, args, node, goal)
    if direct or node is None:
        return direct
    parsed = parse_send_goal(goal)
    if parsed is None or action != "tap_node":
        return None
    if _node_inside_with_label(obs, node, parsed.target):
        return "chat_target"
    if _node_inside_with_label(obs, node, "发送") or _node_inside_with_label(obs, node, "Send"):
        return "send_button"
    if _looks_like_search_node(node) or _node_inside_with_search_label(obs, node):
        return "search_entry"
    return None


def _node_inside_with_label(obs: _Observation, parent: UiNode, label: str) -> bool:
    return _node_with_label_inside(obs, parent, label) is not None


def _node_with_label_inside(obs: _Observation, parent: UiNode, label: str) -> UiNode | None:
    if len(parent.bounds) != 4:
        return None
    l, t, r, b = parent.bounds
    for node in obs.nodes.values():
        if _node_label(node) != label or len(node.bounds) != 4:
            continue
        nl, nt, nr, nb = node.bounds
        if nl >= l and nt >= t and nr <= r and nb <= b:
            return node
    return None


def _node_inside_with_search_label(obs: _Observation, parent: UiNode) -> bool:
    return _search_node_inside(obs, parent) is not None


def _search_node_inside(obs: _Observation, parent: UiNode) -> UiNode | None:
    if len(parent.bounds) != 4:
        return None
    l, t, r, b = parent.bounds
    for node in obs.nodes.values():
        if len(node.bounds) != 4 or not _looks_like_search_node(node):
            continue
        nl, nt, nr, nb = node.bounds
        if nl >= l and nt >= t and nr <= r and nb <= b:
            return node
    return None


def _looks_like_search_node(node: UiNode) -> bool:
    label = _node_label(node)
    haystack = " ".join([node.view_id, node.cls, node.desc, node.text]).lower()
    return label in {"搜索", "搜一搜", "Search"} or "搜索" in label or "search" in haystack


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
