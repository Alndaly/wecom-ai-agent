from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.device import UiNode
from app.models import Robot

log = logging.getLogger(__name__)

_CACHE_DIR = Path("var/react_locator_cache")
_ARTIFACT_DIR = Path("var/react_fallbacks")
_SEND_GOAL_RE = re.compile(
    r"打开与[「\"](?P<target>.+?)[」\"]的聊天，并发送下面这段文本[:：](?P<text>.*)",
    re.DOTALL,
)


@dataclass(frozen=True)
class ParsedSendGoal:
    target: str
    text: str


def parse_send_goal(goal: str) -> ParsedSendGoal | None:
    m = _SEND_GOAL_RE.search(goal or "")
    if not m:
        return None
    target = m.group("target").strip()
    text = m.group("text").strip()
    if not target or not text:
        return None
    return ParsedSendGoal(target=target, text=text)


class LocatorStore:
    def __init__(self, robot: Robot) -> None:
        self.robot = robot
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = _CACHE_DIR / f"{robot.robot_id}.json"
        self.data = self._load()

    def match(
        self,
        role: str,
        nodes: dict[int, UiNode],
        *,
        target: str | None = None,
        screen_size: tuple[int, int] | None = None,
    ) -> UiNode | None:
        entries = [
            e for e in self.data.get("locators", [])
            if e.get("role") == role and e.get("enabled", True)
        ]
        entries.sort(key=lambda e: (int(e.get("success_count") or 0), e.get("updated_at") or ""), reverse=True)
        for entry in entries[:5]:
            node = _match_entry(entry, nodes, target=target, screen_size=screen_size)
            if node is not None:
                log.info(
                    "react locator matched robot=%s role=%s node=%s source=%s",
                    self.robot.robot_id,
                    role,
                    node.id,
                    entry.get("source"),
                )
                return node
        return None

    def remember_success(
        self,
        *,
        role: str,
        action: str,
        node: UiNode,
        obs_meta: dict[str, Any],
        source: str,
        target: str | None = None,
        screen_size: tuple[int, int] | None = None,
    ) -> None:
        if screen_size is None:
            meta_size = obs_meta.get("screen_size") if isinstance(obs_meta, dict) else None
            if isinstance(meta_size, (list, tuple)) and len(meta_size) == 2:
                screen_size = (int(meta_size[0] or 0), int(meta_size[1] or 0))
        locator = _locator_from_node(
            role=role, action=action, node=node, source=source, target=target, screen_size=screen_size
        )
        locator["last_observation"] = obs_meta
        locators = [e for e in self.data.get("locators", []) if e.get("role") != role]
        old = next((e for e in self.data.get("locators", []) if e.get("role") == role), None)
        if old:
            locator["success_count"] = int(old.get("success_count") or 0) + 1
            locator["failure_count"] = int(old.get("failure_count") or 0)
            locator["created_at"] = old.get("created_at") or locator["created_at"]
        locators.append(locator)
        self.data["locators"] = locators
        self.data["updated_at"] = _now()
        self._save()
        log.info(
            "react locator updated robot=%s role=%s action=%s node=%s source=%s",
            self.robot.robot_id,
            role,
            action,
            node.id,
            source,
        )

    def remember_failure(self, *, role: str | None) -> None:
        if not role:
            return
        changed = False
        for entry in self.data.get("locators", []):
            if entry.get("role") != role:
                continue
            entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
            entry["updated_at"] = _now()
            if int(entry["failure_count"]) >= 3:
                entry["enabled"] = False
            changed = True
        if changed:
            self.data["updated_at"] = _now()
            self._save()

    def save_fallback_artifact(
        self,
        *,
        goal: str,
        step_index: int,
        obs_tree: str,
        nodes: dict[int, UiNode],
        screen_size: tuple[int, int],
        screenshot_b64: str | None,
        screenshot_mime: str,
        decision: dict[str, Any],
        ok: bool | None = None,
        message: str | None = None,
    ) -> Path:
        _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = _ARTIFACT_DIR / f"{self.robot.robot_id}-{ts}-step{step_index}"
        meta_path = base.with_suffix(".json")
        image_path: str | None = None
        if screenshot_b64:
            suffix = ".jpg" if "jpeg" in screenshot_mime else ".png"
            img_path = base.with_suffix(suffix)
            img_path.write_bytes(base64.b64decode(screenshot_b64))
            image_path = str(img_path)
        meta = {
            "robot_id": self.robot.robot_id,
            "team_id": self.robot.team_id,
            "created_at": _now(),
            "goal": goal,
            "step_index": step_index,
            "node_count": len(nodes),
            "screen_size": list(screen_size),
            "screenshot_path": image_path,
            "decision": decision,
            "ok": ok,
            "message": message,
            "nodes": [_node_snapshot(n) for n in nodes.values()],
            "tree": obs_tree,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            "react fallback artifact saved robot=%s path=%s nodes=%d screenshot=%s ok=%s",
            self.robot.robot_id,
            meta_path,
            len(nodes),
            bool(image_path),
            ok,
        )
        return meta_path

    def _load(self) -> dict[str, Any]:
        empty = {"version": 2, "robot_id": self.robot.robot_id, "locators": [], "created_at": _now()}
        if not self.path.exists():
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return empty
            # v1 stored bounds_ratio as absolute pixels — drop those entries so
            # they don't get reused on a different-resolution device. The cache
            # rebuilds itself on the next successful action.
            if int(data.get("version") or 1) < 2:
                dropped = len(data.get("locators") or [])
                if dropped:
                    log.info(
                        "react locator cache upgrade robot=%s dropped %d legacy entries",
                        self.robot.robot_id, dropped,
                    )
                data["locators"] = []
                data["version"] = 2
            data.setdefault("locators", [])
            return data
        except Exception as e:  # noqa: BLE001
            log.warning("react locator cache load failed path=%s error=%s", self.path, e)
        return empty

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def role_for_decision(action: str, args: dict[str, Any], node: UiNode | None, goal: str) -> str | None:
    parsed = parse_send_goal(goal)
    if parsed is None:
        return None
    explicit = str(args.get("_locator_role") or "").strip()
    if explicit in {"chat_target", "message_input", "send_button", "search_entry", "search_input"}:
        return explicit
    if action == "input_text":
        text = str(args.get("text") or "")
        if text == parsed.target:
            return "search_input"
        return "message_input"
    if action == "tap_node" and node is not None:
        label = _label(node)
        if label in {"发送", "Send"}:
            return "send_button"
        if label == parsed.target:
            return "chat_target"
        if _looks_like_search_label(label) or _looks_like_search_node(node):
            return "search_entry"
    return None


def _locator_from_node(
    *, role: str, action: str, node: UiNode, source: str, target: str | None,
    screen_size: tuple[int, int] | None,
) -> dict[str, Any]:
    bounds = node.bounds if len(node.bounds) == 4 else [0, 0, 0, 0]
    label = _label(node)
    return {
        "role": role,
        "action": action,
        "source": source,
        "enabled": True,
        "created_at": _now(),
        "updated_at": _now(),
        "success_count": 1,
        "failure_count": 0,
        "sample_node_id": node.id,
        "sample": _node_snapshot(node),
        "match": {
            "cls": node.cls,
            "view_id": node.view_id,
            "text": label if role in {"send_button", "chat_target", "search_entry"} else "",
            "text_mode": "goal_target" if role == "chat_target" else "exact",
            "editable": node.editable,
            "clickable": node.clickable,
            "focusable": node.focusable,
            "scrollable": node.scrollable,
            "bounds_ratio": _to_ratio(bounds, screen_size),
            "target_sample": target,
        },
    }


def _match_entry(
    entry: dict[str, Any], nodes: dict[int, UiNode], *, target: str | None,
    screen_size: tuple[int, int] | None,
) -> UiNode | None:
    spec = entry.get("match") or {}
    scored: list[tuple[int, UiNode]] = []
    for node in nodes.values():
        score = _score_node(spec, node, target=target, screen_size=screen_size)
        if score >= 70:
            scored.append((score, node))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], -item[1].bounds[1] if len(item[1].bounds) == 4 else 0), reverse=True)
    return scored[0][1]


def _score_node(
    spec: dict[str, Any], node: UiNode, *, target: str | None,
    screen_size: tuple[int, int] | None,
) -> int:
    score = 0
    label = _label(node)
    text_mode = spec.get("text_mode")
    expected_text = target if text_mode == "goal_target" else spec.get("text")
    if expected_text:
        if label == expected_text:
            score += 45
        else:
            return 0
    # Require a structural anchor (same class OR same view_id) when the
    # learned locator had one. Without this, a wrapping container that
    # inherits `contentDescription` from a child (e.g. a LinearLayout around
    # the actual "发送" TextView) can match by label + boolean + bounds alone
    # and steal the cache slot. Tapping the container then opens whatever
    # OnClickListener the container has — usually NOT the send button.
    learned_cls = spec.get("cls")
    learned_view_id = spec.get("view_id")
    if learned_cls or learned_view_id:
        cls_ok = bool(learned_cls) and node.cls == learned_cls
        view_id_ok = bool(learned_view_id) and node.view_id == learned_view_id
        if not (cls_ok or view_id_ok):
            return 0
    if learned_view_id and node.view_id == learned_view_id:
        score += 30
    if learned_cls and node.cls == learned_cls:
        score += 18
    for key, weight in (("editable", 25), ("clickable", 12), ("focusable", 8), ("scrollable", 8)):
        val = spec.get(key)
        if val is not None and bool(val) == bool(getattr(node, key)):
            score += weight
    if _bounds_close(spec.get("bounds_ratio"), node.bounds, screen_size):
        score += 16
    return score


def _to_ratio(bounds: list[int], screen_size: tuple[int, int] | None) -> list[float] | None:
    """Normalize absolute pixel bounds to ratio [0,1] of the screen size.

    Without a screen size we cannot normalize safely — return None so the bounds
    contribution is skipped at match time rather than poisoning future matches
    on a different-resolution device.
    """
    if not screen_size:
        return None
    w, h = screen_size
    if w <= 0 or h <= 0 or len(bounds) != 4:
        return None
    l, t, r, b = bounds
    return [round(l / w, 4), round(t / h, 4), round(r / w, 4), round(b / h, 4)]


def _bounds_close(
    expected: Any, actual: list[int], screen_size: tuple[int, int] | None,
) -> bool:
    if not isinstance(expected, list) or len(expected) != 4 or len(actual) != 4:
        return False
    if not screen_size:
        return False
    w, h = screen_size
    if w <= 0 or h <= 0:
        return False
    # Legacy entries stored absolute pixels under the same field name. Any value
    # > 1.5 means it's pixels, not ratio — skip rather than misinterpret.
    if any(float(v) > 1.5 for v in expected):
        return False
    al, at, ar, ab = actual
    actual_ratio = (al / w, at / h, ar / w, ab / h)
    diff = sum(abs(float(e) - float(a)) for e, a in zip(expected, actual_ratio))
    # 0.12 across 4 edges ≈ 3% average drift per edge — tolerates minor layout
    # jitter while rejecting cross-screen-region matches.
    return diff <= 0.12


def _node_snapshot(node: UiNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "cls": node.cls,
        "view_id": node.view_id,
        "text": node.text,
        "desc": node.desc,
        "clickable": node.clickable,
        "focusable": node.focusable,
        "editable": node.editable,
        "scrollable": node.scrollable,
        "bounds": node.bounds,
    }


def _label(node: UiNode) -> str:
    return (node.text or node.desc or "").strip()


def _looks_like_search_label(label: str) -> bool:
    return label in {"搜索", "搜一搜", "Search"} or "搜索" in label or "search" in label.lower()


def _looks_like_search_node(node: UiNode) -> bool:
    haystack = " ".join([node.view_id, node.cls, node.desc, node.text]).lower()
    return "search" in haystack or "搜索" in haystack


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
