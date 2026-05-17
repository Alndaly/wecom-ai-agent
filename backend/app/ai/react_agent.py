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

from app.ai.app_skills import skill_for_package
from app.ai.app_skills_refine import begin_agent_session, end_agent_session
from app.ai.providers import ChatMessage, get_fallback_provider, get_provider
from app.ai.react_locators import (
    LocatorStore,
    parse_open_chat_goal,
    parse_send_goal,
    parse_send_media_goal,
    role_for_decision,
)
from app.ai.react_playbooks import (
    PlaybookStep,
    PlaybookStore,
    kind_for_goal,
    render_playbook_hint,
)
from app.core.config import settings
from app.device import DeviceClient, UiNode
from app.models import Robot
from app.services import settings_service

log = logging.getLogger(__name__)


# Hold references to background tasks spawned by `_persist_playbook` so
# Python 3.11+ doesn't GC them mid-run (asyncio docs: "the event loop only
# keeps weak references to tasks").
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# ---- tool catalogue --------------------------------------------------------
# Action names + descriptions exposed to the LLM. The LLM picks nodes by id
# from the numbered tree; coordinates are never the LLM's concern.
TOOL_SCHEMA = {
    "tap_node": {
        "desc": "点击编号为 node_id 的 UI 节点（看 UI tree 里的 [N] 前缀）。优先用本工具。",
        "args": {"node_id": "int — UI tree 中节点的编号"},
    },
    "long_press_node": {
        "desc": "长按编号为 node_id 的 UI 节点。用于按住说话、长按消息/按钮唤出菜单等场景。",
        "args": {
            "node_id": "int — UI tree 中节点的编号",
            "duration_ms": "可选 int — 长按时长，默认 650ms",
        },
    },
    "double_tap_node": {
        "desc": "双击编号为 node_id 的 UI 节点。用于需要双击打开/放大的控件。",
        "args": {"node_id": "int — UI tree 中节点的编号"},
    },
    "drag_node": {
        "desc": "从一个 UI 节点中心拖拽到另一个 UI 节点中心。用于滑块、排序、拖动卡片等。",
        "args": {
            "from_node_id": "int — 起点节点编号",
            "to_node_id": "int — 终点节点编号",
            "duration_ms": "可选 int — 拖拽时长，默认 450ms",
        },
    },
    "input_text": {
        "desc": "在编号为 node_id 的可编辑节点里输入文本。",
        "args": {
            "node_id": "int — 必须是可编辑的输入框节点",
            "text": "string",
            "mode": "可选 replace|append|clear — 默认 replace；clear 会忽略 text",
        },
    },
    "swipe": {
        "desc": "滑屏。方向四选一：up / down / left / right。一般用于滚动列表。",
        "args": {"direction": "up|down|left|right", "node_id": "可选 int — 在某节点内滑（默认全屏）"},
    },
    "wait_ui": {
        "desc": "等待 UI tree 出现或消失某段文字。用于点击后等弹窗、页面、发送结果刷新。",
        "args": {
            "text": "string — 要等待的文字",
            "absent": "可选 bool — true 表示等待文字消失，默认 false",
            "timeout_ms": "可选 int — 默认 3000ms",
        },
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
    input_panel_visible: bool | None = None
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
    obs_meta: dict[str, Any] = field(default_factory=dict)


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
    # Bump the global ReAct-session counter so background tasks (e.g. app-
    # skill refinement) can defer their own LLM calls and avoid contending
    # for a single-slot inference backend. `finally` covers every exit path
    # including cancellation, timeout, and crash.
    begin_agent_session()
    try:
        return await _run_react_inner(
            db, robot, goal,
            max_steps=max_steps,
            step_timeout=step_timeout,
            log_sink=log_sink,
            force_llm=force_llm,
        )
    finally:
        end_agent_session()


async def _run_react_inner(
    db: AsyncSession,
    robot: Robot,
    goal: str,
    *,
    max_steps: int,
    step_timeout: float,
    log_sink: LogSink,
    force_llm: bool,
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

    await _log(
        "info",
        f"[react] goal={goal!r} max_steps={max_steps} mode={'llm_only' if force_llm else 'rule+llm'}",
    )
    provider = await get_provider(db, robot.team_id)
    fallback_provider = await get_fallback_provider(db, robot.team_id)
    llm_cfg = await settings_service.get(db, robot.team_id, "llm")
    use_vision = _vision_enabled(llm_cfg)
    locator_store = LocatorStore(robot)
    playbook_store = PlaybookStore(robot)
    goal_kind = kind_for_goal(goal)
    playbook_hint = (
        render_playbook_hint(playbook_store.recall(goal_kind) or [])
        if goal_kind
        else ""
    )
    if playbook_hint:
        await _log(
            "info",
            f"[react] playbook hint loaded kind={goal_kind} lines={len(playbook_hint.splitlines())}",
        )
    # Captured from the first observation so the success record can be
    # attributed to the right app-skill regardless of where the run ends
    # (we may drift through `unknown` pkg pages during the run).
    target_package: str | None = None
    # For the media-send phase: snapshot the title of the chat we start in.
    # Phase 2 begins with the target chat already open; if a later `tap_node`
    # drops us back on a CHAT page, we require the title to match this
    # anchor before declaring success — otherwise the agent may have
    # navigated into the *wrong* chat (e.g. via HOME after a stray `back`).
    media_chat_anchor: str | None = None

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

        if target_package is None:
            pkg = _root_package(obs.tree)
            if pkg and pkg != "unknown":
                target_package = pkg
        if (
            media_chat_anchor is None
            and goal_kind == "send_media_phase2"
            and _root_page(obs.tree) == "CHAT"
        ):
            media_chat_anchor = _chat_title(obs)
            if media_chat_anchor:
                await _log(
                    "info",
                    f"[react] media chat anchor captured: {media_chat_anchor!r}",
                )

        # ---- post-action verification (annotate the previous step) ----
        # If the previous step was a send-button tap, re-check the input box
        # state in this fresh observation and write the verdict back into
        # the prior step's message. The LLM sees it on the next render and
        # can decide to try a different node instead of looping.
        if steps and steps[-1].action == "tap_node":
            # Structural cache-decay: if the previous tap was tagged with a
            # role that should have caused a page transition (chat_target,
            # media_picker_entry, ...) but the page label is unchanged, the
            # cached node was misleading. Decay it so the next iteration
            # doesn't keep replaying the same no-op.
            _decay_no_op_locator(steps[-1], obs, locator_store)
            verdict = _post_tap_verdict(
                steps[-1], obs, goal, media_chat_anchor=media_chat_anchor
            )
            if verdict and "[验证]" not in steps[-1].message:
                steps[-1].message = f"{steps[-1].message}  [验证] {verdict}"
                await _log("info", f"[react] step {i-1} post-tap verdict: {verdict}")
        if steps and steps[-1].action == "back":
            verdict = _post_back_verdict(steps[-1], obs)
            if verdict and "[验证]" not in steps[-1].message:
                steps[-1].message = f"{steps[-1].message}  [验证] {verdict}"
                await _log("info", f"[react] step {i-1} post-back verdict: {verdict}")

        # ---- loop detection ----
        # If the last N steps all picked the same action+node and none of
        # them moved the UI (verified by post-tap check), the agent is
        # stuck. Bail before burning the remaining budget.
        if _stuck_opening_wecom(steps):
            return AgentResult(
                ok=False,
                summary="已多次请求打开企业微信但无障碍树仍不可用，请检查手机前台、锁屏/悬浮窗/无障碍状态",
                steps=steps,
            )

        if _stuck_repeating(steps, n=3):
            return AgentResult(
                ok=False,
                summary="检测到连续重复操作未生效，提前结束（请人工核实 UI 状态）",
                steps=steps,
            )

        if _degraded_wecom_observation(obs):
            return AgentResult(
                ok=False,
                summary="企业微信 UI 树不完整（UNKNOWN/节点过少），停止盲操作并等待重试",
                steps=steps,
            )

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
                decision = await _decide(provider, goal, obs, steps, use_vision=use_vision, playbook_hint=playbook_hint)
                if fallback_provider is not None and decision.get("action") == "done" and not decision.get("args", {}).get("success", True):
                    await _log("info", "[react] primary model gave failure; trying fallback model")
                    fallback_decision = await _decide(fallback_provider, goal, obs, steps, use_vision=use_vision, playbook_hint=playbook_hint)
                    if not (fallback_decision.get("action") == "done" and not fallback_decision.get("args", {}).get("success", True)):
                        decision = fallback_decision
            except Exception as e:  # noqa: BLE001
                if fallback_provider is None:
                    await _log("error", f"[react] step {i} llm failed: {e}")
                    return AgentResult(ok=False, summary=f"LLM 调用失败：{e}", steps=steps)
                await _log("warn", f"[react] primary llm failed, trying fallback: {e}")
                try:
                    decision = await _decide(fallback_provider, goal, obs, steps, use_vision=use_vision, playbook_hint=playbook_hint)
                except Exception as fallback_e:  # noqa: BLE001
                    await _log("error", f"[react] fallback llm failed: {fallback_e}")
                    return AgentResult(ok=False, summary=f"LLM 调用失败：{e}; fallback={fallback_e}", steps=steps)

        thought = decision.get("thought") or ""
        action = (decision.get("action") or "").strip()
        args = decision.get("args") or {}
        guarded = _guard_decision(decision, obs, steps)
        if guarded is not None:
            await _log("warn", f"[react] step {i}/{max_steps} guard override action={action} summary={guarded.summary!r}")
            return guarded
        await _log(
            "info",
            f"[react] step {i}/{max_steps} source={decision_source} "
            f"thought={thought!r} action={action} args={_render_log_value(args)}",
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
            if success and goal_kind:
                _persist_playbook(
                    playbook_store, goal_kind, steps,
                    package=target_package, team_id=robot.team_id,
                )
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
        obs_meta = _action_observation_meta(action, args, obs, goal)
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
        steps.append(AgentStep(i, thought, action, args, ok, msg, elapsed, obs_meta=obs_meta))
        await asyncio.sleep(0.4)

    total = int((time.monotonic() - started) * 1000)
    final = await _final_send_verdict(robot, goal, steps)
    if final is not None:
        ok, summary = final
        await _log(
            "info" if ok else "warn",
            f"[react] max_steps final send verdict ok={ok} summary={summary!r}",
        )
        if ok and goal_kind:
            _persist_playbook(
                playbook_store, goal_kind, steps,
                package=target_package, team_id=robot.team_id,
            )
        return AgentResult(ok=ok, summary=summary, steps=steps)
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
        input_panel_visible=dump.input_panel_visible,
        screenshot_b64=screenshot_b64,
        screenshot_mime=screenshot_mime,
    )


async def _final_send_verdict(
    robot: Robot, goal: str, steps: list[AgentStep]
) -> tuple[bool, str] | None:
    if not steps:
        return None
    last = steps[-1]
    if last.action != "tap_node" or not last.ok:
        return None
    role = str((last.args or {}).get("_locator_role") or "")
    if role != "send_button" and "发送" not in (last.message or ""):
        return None
    parsed = parse_send_goal(goal)
    try:
        obs = await _observe(robot, want_screenshot=False)
    except Exception as e:  # noqa: BLE001
        # Avoid duplicate sends: a successful send-button tap is more likely
        # to have sent than failed, so an unobservable confirmation should not
        # trigger retry.
        target = parsed.target if parsed else "目标联系人"
        return True, f"已点击发送按钮，但最终确认失败：{e}；按已发送处理，避免重复发送给 {target}。"
    verdict = _verify_send_cleared(
        obs,
        parsed.text if parsed else "",
        baseline=(last.obs_meta or {}).get("sent_echo_before"),
    )
    target = parsed.target if parsed else "目标联系人"
    if verdict == "sent_echo":
        return True, f"已向 {target} 发送消息。"
    if verdict == "still_filled":
        return False, "已点击发送按钮，但输入框仍含待发送文本，发送未确认。"
    return True, f"已向 {target} 发送消息。"


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
        input_panel_visible=obs.input_panel_visible,
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
    parsed_open = parse_open_chat_goal(goal)
    if parsed is None and parsed_open is None:
        return None, "none"
    assert parsed_open is not None
    open_only = parsed is None
    target = parsed.target if parsed is not None else parsed_open.target
    text = parsed.text if parsed is not None else ""
    failed_cache_roles = {
        str(s.args.get("_locator_role"))
        for s in history
        if not s.ok and s.args.get("_locator_role")
    }

    if not _is_wecom_tree(obs.tree):
        # Some vendors (Huawei EMUI/HarmonyOS, MIUI background-launch limits)
        # silently block `startActivity` from a non-foreground service — the
        # Intent dispatches but WeCom never comes forward. If a launcher icon
        # is visible, tap it via a11y instead (same path a user takes); only
        # fall back to the Intent when no icon is in view.
        icon = _find_launcher_icon(obs, ("企业微信", "WeCom"))
        if icon is not None:
            return {
                "thought": "当前不在企业微信，点击桌面图标进入。",
                "action": "tap_node",
                "args": {"node_id": icon.id},
            }, "rule"
        return {
            "thought": "当前不在企业微信，先切到前台。",
            "action": "open_wecom",
            "args": {},
        }, "rule"

    # Wrong-chat guard. If a message input is visible (we're inside *some*
    # chat) but the title bar doesn't show `target`, we landed in another
    # contact's chat — typically because the operator or a prior task left
    # the device parked there. Sending here would deliver to the wrong
    # customer; back out to the messages list first.
    if (
        _find_message_input(obs) is not None
        and not _last_success(history, "tap_node", locator_role="chat_target")
        and not _in_target_chat(obs, target)
    ):
        return {
            "thought": f"输入框可见，但顶栏不是「{target}」，先返回会话列表。",
            "action": "back",
            "args": {},
        }, "rule"

    # Structural anti-loop guard: if we are already inside the target's
    # chat page, suggesting another `chat_target` tap can only resolve to
    # the title bar (idempotent no-op) and we'll spin until max_steps.
    # Bail to the LLM so it can handle whatever else is in the way —
    # voice-mode toggle, dismiss a popup, scroll the conversation, etc.
    already_in_target_chat = (
        _root_page(obs.tree) == "CHAT"
        and _in_target_chat(obs, target)
        and _find_message_input(obs) is None
    )
    if already_in_target_chat:
        if open_only:
            return {
                "thought": f"已在「{target}」聊天页（顶栏匹配），目标达成。",
                "action": "done",
                "args": {"success": True, "summary": f"已打开与 {target} 的聊天。"},
            }, "rule"
        # Text-send path with no editable input — likely voice mode or
        # similar. Decay the chat_target cache (it was probably learned on
        # the title bar by mistake) and hand control to the LLM.
        locator_store.remember_failure(role="chat_target")
        return None, "none"

    if open_only and _find_message_input(obs) is not None and (
        _in_target_chat(obs, target)
        or _last_success(history, "tap_node", locator_role="chat_target")
    ):
        return {
            "thought": f"目标会话「{target}」已打开。",
            "action": "done",
            "args": {"success": True, "summary": f"已打开与 {target} 的聊天。"},
        }, "rule"

    if open_only and _last_success(history, "tap_node", locator_role="chat_target"):
        message_input = _find_message_input(obs)
        if message_input is not None:
            return {
                "thought": f"目标会话「{target}」已打开。",
                "action": "done",
                "args": {"success": True, "summary": f"已打开与 {target} 的聊天。"},
            }, "rule"

    if open_only and _last_success(history, "input_text", locator_role="search_input"):
        cached_target_after_search = (
            None
            if "chat_target" in failed_cache_roles
            else locator_store.match(
                "chat_target", obs.nodes, target=target, screen_size=obs.screen_size
            )
        )
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

    if open_only and _last_success(history, "tap_node", locator_role="search_entry"):
        cached_search_input = (
            None
            if "search_input" in failed_cache_roles
            else locator_store.match(
                "search_input", obs.nodes, target=target, screen_size=obs.screen_size
            )
        )
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

    if open_only:
        cached_target = (
            None
            if "chat_target" in failed_cache_roles
            else locator_store.match(
                "chat_target", obs.nodes, target=target, screen_size=obs.screen_size
            )
        )
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
        cached_search_entry = (
            None
            if "search_entry" in failed_cache_roles
            else locator_store.match(
                "search_entry", obs.nodes, target=target, screen_size=obs.screen_size
            )
        )
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
                "thought": "聊天列表首屏未找到目标联系人，向上滑动继续寻找。",
                "action": "swipe",
                "args": {"direction": "up"},
            }, "rule"
        return None, "none"

    if _last_success(history, "tap_node", locator_role="send_button"):
        # Verify: a successful tap returns ok=True even when the tap missed
        # (gesture dispatched OK, but the target moved / was hidden). The
        # ground truth is whether the input box is now clear. If the input
        # still contains the goal text, the send did NOT actually happen
        # and we must NOT report success.
        send_step = _last_success_step(history, "tap_node", locator_role="send_button")
        verdict = _verify_send_cleared(
            obs,
            text,
            baseline=((send_step.obs_meta or {}).get("sent_echo_before") if send_step else None),
        )
        if verdict in {"sent_echo", "cleared"}:
            reason = "已看到自己发送的消息气泡" if verdict == "sent_echo" else "输入框已清空"
            return {
                "thought": f"点击发送按钮后{reason}，确认发送成功。",
                "action": "done",
                "args": {"success": True, "summary": f"已向 {target} 发送消息。"},
            }, "rule"
        if verdict == "still_filled":
            # Tap missed — decay any cached send_button locator so the next
            # iteration re-discovers it, and fall through to the LLM path
            # so the model can pick a different node.
            locator_store.remember_failure(role="send_button")
            return None, "none"
        # verdict == "unknown" — no editable input visible. Two possibilities:
        #   (a) The send went through and WeCom briefly hid the input area
        #       (still on CHAT page, just without the soft-keyboard footer).
        #   (b) We tapped a wrong node (e.g. a wrapping LinearLayout that the
        #       cached locator scored as send_button) and got dropped onto
        #       a popup / sidebar / completely different page.
        # Heuristic: trust (a) only if we're still on a WeCom chat page;
        # otherwise decay the locator and bail to the LLM so it can recover.
        if _is_wecom_tree(obs.tree) and _root_page(obs.tree) == "CHAT":
            return {
                "thought": "已点击发送按钮，未找到输入框但仍在聊天页，按成功处理。",
                "action": "done",
                "args": {"success": True, "summary": f"已向 {target} 发送消息。"},
            }, "rule"
        locator_store.remember_failure(role="send_button")
        return None, "none"

    if _last_success(history, "input_text"):
        search_input_done = _last_success(history, "input_text", locator_role="search_input")
        if search_input_done:
            cached_target_after_search = None if "chat_target" in failed_cache_roles else locator_store.match("chat_target", obs.nodes, target=target, screen_size=obs.screen_size)
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

        cached_send = None if "send_button" in failed_cache_roles else locator_store.match("send_button", obs.nodes, target=target, screen_size=obs.screen_size)
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
        cached_message_input = None if "message_input" in failed_cache_roles else locator_store.match("message_input", obs.nodes, target=target, screen_size=obs.screen_size)
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
        cached_search_input = None if "search_input" in failed_cache_roles else locator_store.match("search_input", obs.nodes, target=target, screen_size=obs.screen_size)
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

    cached_input = None if "message_input" in failed_cache_roles else locator_store.match("message_input", obs.nodes, target=target, screen_size=obs.screen_size)
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

    cached_target = None if "chat_target" in failed_cache_roles else locator_store.match("chat_target", obs.nodes, target=target, screen_size=obs.screen_size)
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

    cached_search_entry = None if "search_entry" in failed_cache_roles else locator_store.match("search_entry", obs.nodes, target=target, screen_size=obs.screen_size)
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


def _degraded_wecom_observation(obs: _Observation) -> bool:
    if not _is_wecom_tree(obs.tree):
        return False
    if _root_page(obs.tree) != "UNKNOWN":
        return False
    if len(obs.nodes) > 12:
        return False
    if any(n.editable or n.scrollable for n in obs.nodes.values()):
        return False
    if _recoverable_unknown_observation(obs):
        return False
    labeled = [n for n in obs.nodes.values() if _node_label(n)]
    return len(labeled) <= 2


def _recoverable_unknown_observation(obs: _Observation) -> bool:
    """Return True when a tiny UNKNOWN tree still has an obvious next action.

    WeCom media preview / confirmation pages often expose very small
    accessibility trees: a preview image, maybe a checkbox, and a "发送"
    label. That is not a degraded dump; it is a valid step in the media flow.
    """
    action_labels = {
        "发送",
        "完成",
        "确定",
        "确认",
        "Send",
        "Done",
        "OK",
    }
    for node in obs.nodes.values():
        if _node_label(node) in action_labels:
            return True
        if node.cls == "CheckBox" and (node.clickable or node.focusable):
            return True
    return False


def _chat_title(obs: _Observation) -> str | None:
    """Pick the title-bar label of the currently open chat (if any).

    Looks at nodes sitting in the top ~15% of the screen and returns the
    first non-empty text/desc. Used as a structural anchor — if the agent
    later claims to be "back in chat", we can compare the new title to
    this snapshot to make sure it's the **same** chat, not a different
    one that happens to share the page=CHAT label.
    """
    screen_h = obs.screen_size[1] if obs.screen_size else 0
    if screen_h <= 0:
        return None
    cutoff = int(screen_h * 0.15)
    for n in obs.nodes.values():
        if len(n.bounds) != 4 or n.bounds[1] >= cutoff:
            continue
        label = (n.text or n.desc or "").strip()
        if label:
            return label
    return None


def _in_target_chat(obs: _Observation, target: str) -> bool:
    """True iff the current chat page's title bar shows `target`.

    WeCom renders the conversation title as a TextView pinned to the top of
    the screen (action bar / toolbar region, roughly the top 15% of screen
    height). The same name may appear in message bubbles below, but those
    aren't in the title region — so requiring `bounds.top` to be in the top
    15% reliably distinguishes title from bubble content.
    """
    if not target:
        return False
    screen_h = obs.screen_size[1] if obs.screen_size else 0
    if screen_h <= 0:
        return False
    cutoff = int(screen_h * 0.15)
    for n in obs.nodes.values():
        if len(n.bounds) != 4:
            continue
        if n.bounds[1] >= cutoff:
            continue  # node starts below the title region
        label = (n.text or n.desc or "").strip()
        if label == target:
            return True
    return False


def _find_launcher_icon(obs: _Observation, names: tuple[str, ...]) -> UiNode | None:
    """Find a launcher app icon node matching one of the given labels.

    Prefers the smallest-area matching node so a banner/folder containing the
    label as part of a larger composite doesn't outrank the actual icon.
    """
    candidates: list[UiNode] = []
    for n in obs.nodes.values():
        if len(n.bounds) != 4:
            continue
        label = (n.text or n.desc or "").strip()
        if label in names:
            candidates.append(n)
    if not candidates:
        return None
    clickable = [n for n in candidates if n.clickable]
    pool = clickable or candidates

    def _area(n: UiNode) -> int:
        l, t, r, b = n.bounds
        return max(0, r - l) * max(0, b - t)

    return min(pool, key=_area)


def _stuck_opening_wecom(history: list[AgentStep]) -> bool:
    recent = history[-2:]
    return len(recent) == 2 and all(
        step.action == "open_wecom" and not step.ok for step in recent
    )


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


def _post_tap_verdict(
    prev_step: AgentStep,
    obs: _Observation,
    goal: str,
    *,
    media_chat_anchor: str | None = None,
) -> str | None:
    """After a `tap_node` step, decide whether the UI actually changed in
    the expected way. Returns a short Chinese verdict suitable for inlining
    into the step's `message`, or None when no specific check applies."""
    role = (prev_step.args or {}).get("_locator_role") or ""
    if role != "send_button" and prev_step.action != "tap_node":
        return None
    # Media send has its own success signal: we tapped a "发送" button inside
    # the picker / preview and the screen returned to CHAT (no picker overlay).
    # Detect that here so the agent can short-circuit to done(success=true)
    # instead of looping for more steps after the image has already gone out.
    parsed_media = parse_send_media_goal(goal)
    if parsed_media is not None:
        return _media_post_tap_verdict(prev_step, obs, anchor=media_chat_anchor)
    # The verdicts below reason about the chat input box (was it cleared by
    # the send?), which only makes sense for text-send goals.
    parsed = parse_send_goal(goal)
    if parsed is None:
        return None
    # Only verify when the previous step's intent was clearly send-like.
    # If the locator role is send_button OR the tapped node text contains
    # "发送", we treat it as a send attempt.
    args = prev_step.args or {}
    looks_like_send = (
        role == "send_button"
        or "发送" in str(args)
        or "发送" in (prev_step.message or "")
    )
    if not looks_like_send:
        return None
    baseline = (prev_step.obs_meta or {}).get("sent_echo_before")
    if _find_new_sent_message_echo(obs, parsed.text, baseline) is not None:
        return "已看到自己发送的消息气泡，发送已生效"
    input_node = _find_message_input(obs)
    if input_node is None:
        return "未找到输入框，无法验证"
    cur = (input_node.text or "").strip()
    if not cur:
        return "输入框已清空，发送已生效"
    if parsed.text.strip() in cur:
        return f"输入框仍含待发送文本「{cur}」—— 此次点击未触发发送，请换一个节点尝试或确认按钮是否可用"
    # Parsed goal text is no longer in the input — the previous tap likely
    # sent, and what's left is a different draft.
    return "输入框内容已不再匹配待发送文本，发送可能已生效"


def _media_post_tap_verdict(
    prev_step: AgentStep,
    obs: _Observation,
    *,
    anchor: str | None,
) -> str | None:
    """Purely structural verdict for the media-send phase.

    Three layered checks (all structural, no UI text matching):

      a) Previous step was `tap_node` (we ignore `back`, swipes, etc. —
         backing out of a picker also lands on CHAT but is not a send).
      b) `before_page` was non-CHAT and current page is CHAT.
      c) The current CHAT page's title bar matches the `anchor` captured
         when phase 2 started. Without this, navigating into ANY other
         chat (e.g. via HOME after a stray back) would falsely declare
         success.

    Returns one of three states:
      - success: all three signals line up
      - wrong-chat: returned to a CHAT, but a different one — explicit
        recovery instruction so the LLM doesn't `done` prematurely
      - waiting: tapped from non-CHAT but didn't transition (picker still
        showing upload spinner). Hold position; don't `back`.
    """
    if prev_step.action != "tap_node":
        return None
    before_page = (prev_step.obs_meta or {}).get("before_page")
    cur_page = _root_page(obs.tree)
    if not before_page or before_page == "CHAT":
        return None
    if cur_page == "CHAT":
        cur_title = _chat_title(obs)
        if anchor and cur_title and cur_title != anchor:
            return (
                f"已落到 CHAT 页但顶栏是「{cur_title}」而非起始的「{anchor}」"
                "——这不是目标聊天，发送没有发生（或发到了错误对象）。"
                f"请先 back 回到会话列表，重新打开「{anchor}」核实是否需要重发，"
                "不要直接 done(success=true)。"
            )
        return (
            "页面已从 picker 回到原聊天主页 → 图片已离开选择器，发送已生效，"
            "请立刻调用 done(success=true)"
        )
    # Still in picker / preview / upload. The previous tap likely was the
    # send confirm; UI just hasn't transitioned yet. Tell the agent to wait,
    # not to back out — backing out aborts the send.
    return (
        "已点击媒体发送按钮，但页面仍在 picker/预览/上传状态。"
        "请使用 wait_ui 等待 UI 切回原聊天，不要按 back，也不要再点其它入口。"
    )


def _post_back_verdict(prev_step: AgentStep, obs: _Observation) -> str | None:
    before_panel = _message_bool(prev_step.message, "before_input_panel")
    before_pkg = _message_field(prev_step.message, "before_pkg")
    before_page = _message_field(prev_step.message, "before_page")
    cur_pkg = _root_package(obs.tree)
    cur_page = _root_page(obs.tree)
    cur_panel = _input_panel_visible(obs)
    parts: list[str] = []
    if before_panel is True and not cur_panel:
        parts.append("输入面板已消失")
    elif before_panel is False and not cur_panel:
        parts.append("执行前后均未检测到输入面板")
    elif before_panel is True and cur_panel:
        parts.append("输入面板仍存在")
    if before_pkg and cur_pkg and before_pkg != cur_pkg:
        parts.append(f"上下文从 pkg={before_pkg} 变为 pkg={cur_pkg}")
    elif before_page and cur_page and before_page != cur_page:
        parts.append(f"上下文从 page={before_page} 变为 page={cur_page}")
    return "；".join(parts) if parts else None


def _stuck_repeating(steps: list[AgentStep], *, n: int = 3) -> bool:
    """True iff the last `n` steps were the same action with the same args
    AND nothing structurally changed between them (no page transition).

    Detection is purely positional: same `(action, node_id)` signature
    repeated N times *and* the page snapshot taken before each of those
    actions is the same value. If the page never moved the tap is a no-op
    and we're spinning — typical example is mis-cached `chat_target`
    pointing at the title bar of the already-open chat.
    """
    if len(steps) < n:
        return False
    tail = steps[-n:]
    head = tail[0]
    sig = (head.action, head.args.get("node_id"))
    if not all((s.action, s.args.get("node_id")) == sig for s in tail):
        return False
    pages = [(s.obs_meta or {}).get("before_page") for s in tail]
    return all(p is not None for p in pages) and len(set(pages)) == 1


def _verify_send_cleared(
    obs: _Observation,
    sent_text: str,
    baseline: dict[str, int] | None = None,
) -> str:
    """Return 'sent_echo' if the sent bubble is visible, 'cleared' if the
    message input no longer holds `sent_text`, 'still_filled' if it does
    (tap missed), 'unknown' if we couldn't even find the input box."""
    if _find_new_sent_message_echo(obs, sent_text, baseline) is not None:
        return "sent_echo"
    node = _find_message_input(obs)
    if node is None:
        return "unknown"
    cur = (node.text or "").strip()
    if not cur:
        return "cleared"
    needle = (sent_text or "").strip()
    if needle and needle in cur:
        return "still_filled"
    # Different text entirely (rare — would mean someone else is typing).
    # Treat as cleared so we don't loop on a phantom mismatch.
    return "cleared"


def _find_sent_message_echo(obs: _Observation, sent_text: str) -> UiNode | None:
    candidates = _sent_message_echo_candidates(obs, sent_text)
    if not candidates:
        return None
    return max(candidates, key=lambda n: n.bounds[1])


def _sent_message_echo_stats(obs: _Observation, sent_text: str) -> dict[str, int]:
    candidates = _sent_message_echo_candidates(obs, sent_text)
    if not candidates:
        return {"count": 0, "max_bottom": 0}
    return {"count": len(candidates), "max_bottom": max(n.bounds[3] for n in candidates)}


def _find_new_sent_message_echo(
    obs: _Observation,
    sent_text: str,
    baseline: dict[str, int] | None,
) -> UiNode | None:
    if baseline is None:
        return None
    candidates = _sent_message_echo_candidates(obs, sent_text)
    if not candidates:
        return None
    before_count = int(baseline.get("count") or 0)
    before_bottom = int(baseline.get("max_bottom") or 0)
    newest = max(candidates, key=lambda n: n.bounds[3])
    if len(candidates) > before_count:
        return newest
    if before_count > 0 and newest.bounds[3] > before_bottom + 16:
        return newest
    return None


def _sent_message_echo_candidates(obs: _Observation, sent_text: str) -> list[UiNode]:
    needle = (sent_text or "").strip()
    if not needle:
        return []
    screen_w, screen_h = obs.screen_size if obs.screen_size else (0, 0)
    candidates: list[UiNode] = []
    for node in obs.nodes.values():
        if len(node.bounds) != 4:
            continue
        if (node.text or "").strip() != needle:
            continue
        if screen_w > 0 and screen_h > 0:
            l, t, r, b = node.bounds
            center_x = (l + r) / 2
            if center_x < screen_w * 0.42:
                continue
            if t < screen_h * 0.10 or b > screen_h * 0.94:
                continue
        candidates.append(node)
    return candidates


def _find_message_input(obs: _Observation) -> UiNode | None:
    candidates = [
        n for n in obs.nodes.values()
        if n.editable and len(n.bounds) == 4
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda n: n.bounds[1])


def _input_panel_visible(obs: _Observation) -> bool:
    return bool(obs.input_panel_visible)


def _root_package(tree: str) -> str:
    first = tree.splitlines()[0] if tree else ""
    m = re.search(r"pkg=([^\s]+)", first)
    return m.group(1) if m else "unknown"


def _root_page(tree: str) -> str:
    first = tree.splitlines()[0] if tree else ""
    m = re.search(r"page=([^\s=]+)", first)
    return m.group(1) if m else "unknown"


def _message_field(message: str, key: str) -> str | None:
    m = re.search(rf"\b{re.escape(key)}=([^\s)]+)", message or "")
    return m.group(1) if m else None


def _message_bool(message: str, key: str) -> bool | None:
    value = _message_field(message, key)
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _guard_decision(decision: dict[str, Any], obs: _Observation, steps: list[AgentStep]) -> AgentResult | None:
    action = (decision.get("action") or "").strip()
    if action != "back":
        return None
    last_back = next((s for s in reversed(steps) if s.action == "back" and s.ok), None)
    if last_back is None:
        return None
    msg = last_back.message or ""
    page_changed = "上下文从" in msg
    panel_collapsed = "输入面板已消失" in msg
    # Android's BACK has two-stage semantics: first dismiss the soft keyboard,
    # then leave the activity. So "panel collapsed but page unchanged" means
    # the previous back only closed the keyboard — let the next back actually
    # navigate. We only declare back-as-success when the page truly changed.
    if panel_collapsed and page_changed:
        return AgentResult(ok=True, summary="返回键已收起输入面板，停止继续返回。", steps=steps)
    if panel_collapsed and not page_changed:
        return None  # allow another back to actually leave the page
    if "执行前后均未检测到输入面板" in msg or page_changed:
        return AgentResult(ok=False, summary="连续返回没有明确收益，已阻止继续返回。", steps=steps)
    recent_back_count = sum(1 for s in steps[-3:] if s.action == "back" and s.ok)
    if recent_back_count >= 2:
        return AgentResult(ok=False, summary="检测到连续返回操作，已阻止继续返回。", steps=steps)
    return None


def _find_search_input(obs: _Observation) -> UiNode | None:
    candidates = [
        n for n in obs.nodes.values()
        if n.editable and len(n.bounds) == 4
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


def _last_success_step(
    history: list[AgentStep],
    action: str,
    *,
    locator_role: str | None = None,
) -> AgentStep | None:
    for step in reversed(history):
        if step.action != action:
            continue
        if not step.ok:
            return None
        if locator_role and step.args.get("_locator_role") != locator_role:
            continue
        return step
    return None


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
1. 只操作 UI tree 里已经列出的节点。优先用 tap_node / long_press_node / double_tap_node / drag_node 而不是猜测坐标。
2. 节点没有可见文字（例如只是个 ImageView 图标）时，结合截图判断它的语义。
3. 如果当前 root 包名不是 com.tencent.wework，第一步必须用 open_wecom。
4. 找不到目标节点时，先判断是否处在错误页面层级、弹窗/键盘/详情页遮挡、或刚才点击后进入了无关页面：
   - 如果只是列表未露出目标，先 swipe 滚动。
   - 如果明显走进了错误页面、被弹窗/键盘挡住、当前页面不可能完成目标，或连续操作没有收益，可以先用 back 退回上一层再重新观察。
   - back 后必须根据新的 UI tree 重新规划；连续 2~3 步仍无进展则用 done(success=false) 退出，不要硬猜。
5. 一次只输出一个动作。
6. **`tap_node` 返回 ok=True 只代表手势派发成功，不代表操作真的生效**。在 done(success=true) 之前必须看当前 UI tree 验证：
   - 发送类操作：消息输入框 (editable, 屏幕下半) 的 text 应该已经清空 / 变为占位符（如 "发消息或按住..."）。如果还含有你刚发送的文本 → 说明点错按钮了，**不要 done(success=true)**，换个节点重试或返回 done(success=false)。
   - 跳转类操作：当前页面的标志性节点应该变了（顶栏标题 / tab 选中状态）。
   - **发送相册图片**：唯一可靠的成功信号是 UI tree 的 `page` 字段从非 CHAT（相册/预览页通常是 picker 或 UNKNOWN）**切回** CHAT。一旦观察到这个切换，立刻 done(success=true)。**不要重复点附件入口、再次进入相册、或反复点发送**——会导致同一张图被多次发送。
   - 看到 `[验证] ...` 字样的消息是后端对前一步执行结果的结构化判断：**优先采信结论**，比自己揣摩 UI 更可靠。
7. 当 UI 文案/目标包含“长按”“按住”“hold”“press and hold”，或控件语义明显需要持续按压（例如按住说话、长按消息菜单），使用 long_press_node。
8. 点击、输入、拖拽后如果页面需要时间变化，可以用 wait_ui 等待关键文字出现/消失，再判断是否 done。
9. 输入框操作按语义选择 input_text 的 mode：默认 replace；需要保留原文字时 append；需要清空时 clear。
10. 根据【当前状态】判断目标是否已达成：如果目标是键盘/输入面板相关，而 input_panel_visible=false，必须 done(success=true)，不要再点发送、输入框右侧图标或 back。
11. back 是“脱困/回退上一层”的工具，不是常规导航捷径：实在无法解决、找不到目标、页面层级明显不对、弹窗/输入面板阻塞操作时可以考虑 back；但最近一次 back 已经让输入面板消失，或让 root_package/page 发生非预期变化时，不要继续 back。
11.5 已在目标 CHAT 页但找不到可编辑输入框（EditText / editable=true 的节点）时，输入栏可能处于非文本模式（如长按说话）。处理思路是结构化的：输入栏底部通常是一组并排的 ImageView / TextView，按 UI tree 顺序排列，里面有一个负责模式切换的图标。可以从该组节点里按顺序逐个尝试 tap_node；每次点完看 UI tree 是否出现 editable=true 的节点，出现即说明切换成功，可以继续 input_text。不要靠图标文字猜，按位置依次试。
12. 如果动作对应可复用 UI 位置，请在 args 里额外写 `_locator_role`，取值只能是：
   - chat_target：目标联系人/搜索结果会话
   - search_entry：搜索入口/搜索图标/搜索框占位入口
   - search_input：搜索页输入框
   - message_input：聊天页消息输入框
   - send_button：发送按钮
   - compose_plus：聊天页输入栏里用于展开附件面板的入口（一组并排 ImageView 中的某一个；点错会弹出表情/语音/键盘切换，按 UI tree 顺序换同行里其它图标继续试）
   - media_picker_entry：附件面板里通往相册的格子（面板里一组并排的图标 + 文字组合，按 UI tree 顺序选 label 对应相册/图片的那个）
   - gallery_first_item：相册网格里**第一个没有任何文字 label / 纯 ImageView 的缩略图节点**。网格按 UI tree 顺序排列，靠前的格子如果带文字 label（即同一格内有 TextView 子节点），那是相机/快捷工具入口，不要选；从其后第一个纯 ImageView 开始才是真实照片，最新存入的一张通常就在那里。
   - gallery_send_button：相册或预览页右下角的确认发送按钮。**只勾选缩略图不会发送**，必须再点这个；若点缩略图后进入了预览/编辑页，预览页右下角的发送按钮同样用此 role。未选图时通常是禁用态。"""


async def _decide(
    provider,
    goal: str,
    obs: _Observation,
    history: list[AgentStep],
    *,
    use_vision: bool,
    playbook_hint: str = "",
) -> dict[str, Any]:
    # System prompt = tool catalogue + app handbook. Both are stable across
    # every step of the run, so they go into the `system` slot where remote
    # providers can cache the prefix and we avoid re-billing thousands of
    # tokens per ReAct iteration. The dynamic per-step content (goal,
    # history, current UI tree) stays in `user` where it has to.
    skill = skill_for_package(_root_package(obs.tree))
    sys_parts = [_SYSTEM_PROMPT.format(tools=_tools_block())]
    if skill:
        sys_parts.append(
            "\n\n"
            + skill.as_prompt_block(
                "操作手册（描述结构与位置；具体节点请按当前 UI tree 判断）"
            )
        )
    sys = "".join(sys_parts)

    hist_lines = []
    for s in history[-5:]:
        hist_lines.append(
            f"#{s.index} action={s.action} args={_render_log_value(s.args)} ok={s.ok} msg={s.message!r}"
        )
    hist = "\n".join(hist_lines) if hist_lines else "（无）"
    state = _state_summary(obs)
    playbook_block = (
        f"【过往成功步骤参考】（来自历史成功执行；仅作参考，当前 UI tree 与之冲突时以 UI tree 为准）\n{playbook_hint}\n\n"
        if playbook_hint
        else ""
    )
    user_text = (
        f"【目标】{goal}\n\n"
        f"{playbook_block}"
        f"【最近的执行历史】\n{hist}\n\n"
        f"【当前状态】\n{state}\n\n"
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
        log.warning("[react] bad_json raw=%r model=%s", text, result.model)
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
        ack = await device.tap_node(node.id, cx, cy, expected=_node_expectation(node), timeout=step_timeout)
        label = _node_label(node)
        label_part = f" label={label!r}" if label else ""
        return ack.ok, f"tap_node({node.id}{label_part}) node_action_or_xy=({cx},{cy}) -> {ack.message or ''}"

    if action == "long_press_node":
        node = _lookup_node(obs, args.get("node_id"))
        if node is None:
            return False, f"node_id={args.get('node_id')} 不在 UI tree 中"
        cx, cy = node.center
        duration_ms = _coerce_duration_ms(args.get("duration_ms"), default=650, min_ms=350, max_ms=3000)
        ack = await device.long_press_node(
            node.id,
            cx,
            cy,
            expected=_node_expectation(node),
            duration_ms=duration_ms,
            timeout=step_timeout,
        )
        label = _node_label(node)
        label_part = f" label={label!r}" if label else ""
        return ack.ok, (
            f"long_press_node({node.id}{label_part}) "
            f"node_action_or_xy=({cx},{cy}) duration={duration_ms}ms -> {ack.message or ''}"
        )

    if action == "double_tap_node":
        node = _lookup_node(obs, args.get("node_id"))
        if node is None:
            return False, f"node_id={args.get('node_id')} 不在 UI tree 中"
        cx, cy = node.center
        ack = await device.double_tap_node(node.id, cx, cy, expected=_node_expectation(node), timeout=step_timeout)
        label = _node_label(node)
        label_part = f" label={label!r}" if label else ""
        return ack.ok, f"double_tap_node({node.id}{label_part}) node_action_or_xy=({cx},{cy}) -> {ack.message or ''}"

    if action == "drag_node":
        from_node = _lookup_node(obs, args.get("from_node_id"))
        to_node = _lookup_node(obs, args.get("to_node_id"))
        if from_node is None:
            return False, f"from_node_id={args.get('from_node_id')} 不在 UI tree 中"
        if to_node is None:
            return False, f"to_node_id={args.get('to_node_id')} 不在 UI tree 中"
        x1, y1 = from_node.center
        x2, y2 = to_node.center
        duration_ms = _coerce_duration_ms(args.get("duration_ms"), default=450, min_ms=120, max_ms=5000)
        ack = await device.drag_xy(x1, y1, x2, y2, duration_ms=duration_ms, timeout=step_timeout)
        return ack.ok, (
            f"drag_node({from_node.id}->{to_node.id}) "
            f"xy=({x1},{y1})->({x2},{y2}) duration={duration_ms}ms -> {ack.message or ''}"
        )

    if action == "input_text":
        node_id = args.get("node_id")
        text = args.get("text") or ""
        mode = _coerce_input_mode(args.get("mode"))
        expected: dict[str, Any] | None = None
        resolved_node_id: int | None = None
        if node_id is not None:
            node = _lookup_node(obs, node_id)
            if node is None:
                return False, f"node_id={node_id} 不在 UI tree 中"
            if not node.editable:
                return False, f"node {node_id} 不可编辑（cls={node.cls}）"
            resolved_node_id = node.id
            expected = _node_expectation(node)
        ack = await device.input_text(
            text,
            node_id=resolved_node_id,
            expected=expected,
            mode=mode,
            timeout=step_timeout,
        )
        return ack.ok, f"input_text(mode={mode}) -> {ack.message or ''}"

    if action == "swipe":
        direction = (args.get("direction") or "up").lower()
        target_node = _lookup_node(obs, args.get("node_id"))
        x1, y1, x2, y2 = _swipe_coords(direction, obs, target_node)
        ack = await device.swipe(x1, y1, x2, y2, duration_ms=280, timeout=step_timeout)
        return ack.ok, ack.message or ""

    if action == "wait_ui":
        return await _wait_ui(robot, args)

    if action == "back":
        ack = await device.back(timeout=step_timeout)
        return ack.ok, (
            f"{ack.message or ''} "
            f"(before_pkg={_root_package(obs.tree)} before_page={_root_page(obs.tree)} "
            f"before_input_panel={str(_input_panel_visible(obs)).lower()})"
        )

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


def _action_observation_meta(
    action: str,
    args: dict[str, Any],
    obs: _Observation,
    goal: str,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        # Structural snapshot of the page label *before* the action runs.
        # Verdicts compare it against the post-action observation to detect
        # transitions (e.g. picker → CHAT means a media send completed)
        # without resorting to text matching against specific UI strings.
        "before_page": _root_page(obs.tree),
    }
    role = str(args.get("_locator_role") or "")
    if action == "tap_node" and role == "send_button":
        parsed = parse_send_goal(goal)
        if parsed is not None:
            meta["sent_echo_before"] = _sent_message_echo_stats(obs, parsed.text)
    return meta


def _node_expectation(node: UiNode) -> dict[str, Any]:
    return {
        "cls": node.cls,
        "view_id": node.view_id,
        "text": node.text,
        "desc": node.desc,
        "bounds": node.bounds,
        "editable": node.editable,
        "clickable": node.clickable,
    }


def _coerce_duration_ms(value: Any, *, default: int, min_ms: int, max_ms: int) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = default
    return max(min_ms, min(max_ms, duration))


def _coerce_input_mode(value: Any) -> str:
    mode = str(value or "replace").strip().lower()
    if mode in {"replace", "append", "clear"}:
        return mode
    return "replace"


async def _wait_ui(robot: Robot, args: dict[str, Any]) -> tuple[bool, str]:
    text = str(args.get("text") or "").strip()
    if not text:
        return False, "wait_ui 缺少 text"
    absent = bool(args.get("absent", False))
    timeout_ms = _coerce_duration_ms(args.get("timeout_ms"), default=3000, min_ms=300, max_ms=15000)
    deadline = time.monotonic() + timeout_ms / 1000
    last_seen = False
    attempts = 0
    while True:
        attempts += 1
        obs = await _observe(robot, want_screenshot=False)
        last_seen = _tree_has_text(obs, text)
        if last_seen != absent:
            state = "消失" if absent else "出现"
            return True, f"wait_ui text={text!r} 已{state} attempts={attempts}"
        if time.monotonic() >= deadline:
            state = "仍存在" if last_seen else "仍未出现"
            return False, f"wait_ui timeout text={text!r} {state} attempts={attempts}"
        await asyncio.sleep(0.25)


def _tree_has_text(obs: _Observation, text: str) -> bool:
    needle = text.strip()
    if not needle:
        return False
    for node in obs.nodes.values():
        if needle in (node.text or "") or needle in (node.desc or ""):
            return True
    return needle in obs.tree


def _state_summary(obs: _Observation) -> str:
    input_node = _find_message_input(obs)
    input_text = (input_node.text or "").strip() if input_node is not None else ""
    return "\n".join([
        f"- root_package={_root_package(obs.tree)}",
        f"- page={_root_page(obs.tree)}",
        f"- input_panel_visible={str(_input_panel_visible(obs)).lower()}",
        f"- focused_editable={str(bool(input_node and input_node.focusable)).lower()}",
        f"- message_input_text={input_text!r}",
    ])


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
    # Media phase 2 — no hardcoded label inference. Roles for the media flow
    # are caching keys for repeat traversals, and we'd rather not pin them
    # to literal "发送" / "图片" strings (those vary across WeCom versions).
    # If the LLM tagged `_locator_role` explicitly we honour it via
    # `role_for_decision` above; otherwise the playbook still records the
    # action sequence and primes the next run via the prompt hint.
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
        l, t, r, b = _swipe_region(obs)
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


def _swipe_region(obs: _Observation) -> tuple[int, int, int, int]:
    w, h = obs.screen_size
    if w > 0 and h > 0:
        return 0, int(h * 0.15), w, int(h * 0.85)

    bounds = [node.bounds for node in obs.nodes.values() if len(node.bounds) == 4]
    if not bounds:
        return 0, 0, 1, 1

    left = min(b[0] for b in bounds)
    top = min(b[1] for b in bounds)
    right = max(b[2] for b in bounds)
    bottom = max(b[3] for b in bounds)
    height = max(1, bottom - top)
    return left, top + int(height * 0.15), right, top + int(height * 0.85)


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
        "args": {"success": False, "summary": f"bad_json: {s}"},
    }


def _render_log_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# Roles whose semantics include a page transition: a successful tap should
# move the device to a new screen. If the page label stays the same after
# such a tap, the cached node was almost certainly wrong (e.g. chat_target
# pointing at the chat's title bar instead of a list entry).
_NAV_ROLES = frozenset(
    {
        "chat_target",
        "search_entry",
        "compose_plus",
        "media_picker_entry",
        "gallery_send_button",
    }
)


def _decay_no_op_locator(
    prev_step: AgentStep, obs: _Observation, store: LocatorStore
) -> None:
    if not prev_step.ok:
        return
    role = str((prev_step.args or {}).get("_locator_role") or "")
    if role not in _NAV_ROLES:
        return
    before_page = (prev_step.obs_meta or {}).get("before_page")
    if not before_page:
        return
    if _root_page(obs.tree) != before_page:
        return  # page actually transitioned — keep the cache entry
    store.remember_failure(role=role)
    log.info(
        "react locator decay: role=%s tap registered no page transition (page=%s)",
        role,
        before_page,
    )


def _persist_playbook(
    store: PlaybookStore,
    kind: str,
    steps: list[AgentStep],
    *,
    package: str | None = None,
    team_id: int | None = None,
) -> None:
    """Distill a successful run into a replayable trace.

    We only keep steps that actually executed against the device (ok=True,
    skipping the terminal `done`). The summary line favors the LLM's
    `thought` over the raw exec message — the thought captures *why* the
    step was chosen, which is what future runs need to mirror the
    reasoning even when the UI shifts.

    When `package` is known, also feeds the app-skill self-refinement
    pipeline so the per-app handbook can grow from accumulated successes.
    """
    distilled: list[PlaybookStep] = []
    next_idx = 1
    for step in steps:
        if step.action == "done":
            continue
        if not step.ok:
            continue
        role = str((step.args or {}).get("_locator_role") or "") or None
        summary = (step.thought or step.message or "").strip()
        distilled.append(
            PlaybookStep(
                index=next_idx,
                action=step.action,
                role=role,
                summary=summary,
            )
        )
        next_idx += 1
    if not distilled:
        return
    store.remember_success(kind, distilled)
    if package and team_id is not None:
        try:
            from app.ai.app_skills_refine import record_success

            task = asyncio.create_task(
                record_success(
                    package=package,
                    team_id=team_id,
                    goal_kind=kind,
                    steps=[s.to_dict() for s in distilled],
                ),
                name=f"skill-record-{package}",
            )
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
        except Exception:  # noqa: BLE001
            log.debug("app skill record_success dispatch failed", exc_info=True)
