"""Episodic memory for the ReAct agent — successful step trajectories.

Complements ``react_locators`` (which caches per-role node features for "where
to click"). A playbook records *the sequence of actions/roles* that worked for
a given goal kind, so future runs can be primed with "last time this worked
in N steps: A → B → C → done". The LLM is told the playbook is advisory and
that the live UI tree wins when they disagree.

Storage: ``var/react_playbooks/{robot_id}.json``. One file per device.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ai.react_locators import (
    parse_open_chat_goal,
    parse_send_goal,
    parse_send_media_goal,
)
from app.models import Robot

log = logging.getLogger(__name__)

_PLAYBOOK_DIR = Path("var/react_playbooks")
_MAX_PER_KIND = 5
_MAX_STEPS_RECORDED = 16
_SUMMARY_MAX_CHARS = 140


def kind_for_goal(goal: str) -> str | None:
    """Bucket a goal string into a playbook kind, or None if too generic.

    Order matters: send_media_phase2's regex matches the literal "文件名 X"
    suffix that only the orchestrator's phase-C goal carries, so it can never
    collide with send_text/open_chat goals.
    """
    if parse_send_media_goal(goal):
        return "send_media_phase2"
    if parse_send_goal(goal):
        return "send_text"
    if parse_open_chat_goal(goal):
        return "open_chat"
    return None


@dataclass(frozen=True)
class PlaybookStep:
    index: int
    action: str
    role: str | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "role": self.role,
            "summary": self.summary,
        }


class PlaybookStore:
    def __init__(self, robot: Robot) -> None:
        self.robot = robot
        _PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        self.path = _PLAYBOOK_DIR / f"{robot.robot_id}.json"
        self.data = self._load()

    def recall(self, kind: str) -> list[PlaybookStep] | None:
        """Return the best-known playbook for `kind`, or None if untrained.

        "Best" is the entry with highest success_count, ties broken by
        recency. We only return the top one — feeding multiple variants to
        the LLM tends to confuse rather than help.
        """
        plays = list((self.data.get("playbooks") or {}).get(kind) or [])
        if not plays:
            return None
        plays.sort(
            key=lambda p: (int(p.get("success_count") or 0), p.get("updated_at") or ""),
            reverse=True,
        )
        best = plays[0]
        steps = best.get("steps") or []
        return [
            PlaybookStep(
                index=int(s.get("index") or 0),
                action=str(s.get("action") or ""),
                role=(s.get("role") or None),
                summary=str(s.get("summary") or "")[:_SUMMARY_MAX_CHARS],
            )
            for s in steps
        ]

    def remember_success(self, kind: str, steps: list[PlaybookStep]) -> None:
        """Bump or insert a playbook for a successful run.

        Entries are keyed by their (action, role) signature so the same shape
        of run reinforces a single bucket rather than fragmenting. When a new
        signature appears and the per-kind cap is full, the lowest-scoring
        existing entry is evicted.
        """
        if not steps:
            return
        kept = steps[:_MAX_STEPS_RECORDED]
        sig = tuple((s.action, s.role or "") for s in kept)
        playbooks = self.data.setdefault("playbooks", {})
        plays = playbooks.setdefault(kind, [])
        match: dict[str, Any] | None = None
        for play in plays:
            play_sig = tuple(
                (s.get("action") or "", s.get("role") or "")
                for s in (play.get("steps") or [])
            )
            if play_sig == sig:
                match = play
                break
        if match is not None:
            match["success_count"] = int(match.get("success_count") or 0) + 1
            match["updated_at"] = _now()
            # Refresh step summaries — action/role are unchanged but the
            # human-readable thought may evolve as the model gets better.
            match["steps"] = [s.to_dict() for s in kept]
        else:
            plays.append(
                {
                    "steps": [s.to_dict() for s in kept],
                    "success_count": 1,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            )
            plays.sort(
                key=lambda p: (
                    int(p.get("success_count") or 0),
                    p.get("updated_at") or "",
                ),
                reverse=True,
            )
            if len(plays) > _MAX_PER_KIND:
                playbooks[kind] = plays[:_MAX_PER_KIND]
        self.data["updated_at"] = _now()
        self._save()
        log.info(
            "react playbook updated robot=%s kind=%s steps=%d %s",
            self.robot.robot_id,
            kind,
            len(kept),
            "reinforced" if match else "new",
        )

    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "version": 1,
            "robot_id": self.robot.robot_id,
            "playbooks": {},
            "created_at": _now(),
        }
        if not self.path.exists():
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return empty
            data.setdefault("playbooks", {})
            return data
        except Exception as e:  # noqa: BLE001
            log.warning(
                "react playbook load failed path=%s error=%s", self.path, e
            )
            return empty

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)


def render_playbook_hint(steps: list[PlaybookStep]) -> str:
    """Render a recalled playbook as a compact bullet list for the LLM.

    Roles are surfaced because they're the bridge to the locator cache —
    when the LLM emits `_locator_role` matching a learned role, future runs
    short-circuit to the cached node.
    """
    if not steps:
        return ""
    lines: list[str] = []
    for s in steps:
        role = f"（_locator_role={s.role}）" if s.role else ""
        summary = s.summary.replace("\n", " ").strip()
        if summary:
            lines.append(f"  {s.index}. {s.action}{role} — {summary}")
        else:
            lines.append(f"  {s.index}. {s.action}{role}")
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
