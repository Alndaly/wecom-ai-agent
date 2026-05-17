"""Self-growing app skill documents.

Each successful ReAct run for a given Android package contributes a trace
(action sequence + role tags + thought summaries). Once enough new traces
accumulate the LLM is asked to rewrite the package's handbook based on:

  - the **current** handbook (preserved as the structural baseline)
  - the **recent successful traces** (what actually worked lately)

The refined handbook overwrites the active `<package>.md`; the previous
version is archived to `.archive/<package>/<timestamp>-vN.md` so rollback is
always one file move away. State + raw success log live in the same archive
folder.

Layout under `app/ai/app_skills/`:

    com.tencent.wework.md                              ← active (loaded)
    .archive/
      com.tencent.wework/
        state.json                                     ← counters, version
        success_log.jsonl                              ← every recorded success
        20260517T072000Z-v1.md                         ← old handbook
        20260517T083000Z-v2.md                         ← older still

Refinement is best-effort and runs in the background — if the LLM call
fails the active handbook is untouched and the next successful run will
retry. A per-package asyncio.Lock prevents concurrent refinements.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ai.app_skills import reload_skills

log = logging.getLogger(__name__)

_SKILL_DIR = Path(__file__).with_name("app_skills")
_ARCHIVE_ROOT = _SKILL_DIR / ".archive"
_FRONTMATTER_SEP = "\n---\n"

# Tunables. Conservative defaults — refinement is an LLM call so don't let
# it fire too often.
DEFAULT_REFINE_THRESHOLD = 10
SUCCESS_LOG_FOR_REFINEMENT = 30  # how many recent successes to feed the LLM
SUCCESS_LOG_KEEP = 200  # cap log file so it doesn't grow unbounded
MAX_HANDBOOK_CHARS = 6_000  # rough ceiling we ask the model to respect

# Refinement runs an LLM call against the same provider the ReAct agent
# uses. Behaviour scales with what the provider can do concurrently:
#   - Single-slot backends (Ollama, llama.cpp single-instance) need
#     `react_refine_max_concurrent_agents = 0` so refine waits for full
#     idleness — otherwise the agent's step queues behind us and times out.
#   - Remote providers (OpenAI, Anthropic, multi-slot self-hosted) accept
#     concurrent requests, so refine can ride alongside a handful of
#     in-flight agents. Default `= 2` keeps a couple of slots free for any
#     burst while still letting refinement fire promptly.
REFINE_WAIT_TOTAL_SEC = 600  # give up after this; counter stays high and retries on next success
REFINE_POLL_SEC = 3

_PACKAGE_LOCKS: dict[str, asyncio.Lock] = {}
_ACTIVE_AGENT_SESSIONS = 0
# Module-scoped task registry: keeps fire-and-forget refinement tasks from
# being GC'd before they finish (which would silently drop the work and
# trigger 'Task was destroyed but it is pending' warnings on Python 3.11+).
_PENDING_REFINE_TASKS: set[asyncio.Task] = set()


def begin_agent_session() -> None:
    """Bump the in-flight ReAct counter; call from `run_react` entry."""
    global _ACTIVE_AGENT_SESSIONS
    _ACTIVE_AGENT_SESSIONS += 1


def end_agent_session() -> None:
    """Decrement on `run_react` exit (success or failure, via finally)."""
    global _ACTIVE_AGENT_SESSIONS
    _ACTIVE_AGENT_SESSIONS = max(0, _ACTIVE_AGENT_SESSIONS - 1)


def active_agent_sessions() -> int:
    return _ACTIVE_AGENT_SESSIONS


@dataclass
class _PackageState:
    successes_since_last_refine: int = 0
    refine_threshold: int = DEFAULT_REFINE_THRESHOLD
    version_count: int = 0
    last_refined_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes_since_last_refine": self.successes_since_last_refine,
            "refine_threshold": self.refine_threshold,
            "version_count": self.version_count,
            "last_refined_at": self.last_refined_at,
        }


async def record_success(
    *,
    package: str,
    team_id: int,
    goal_kind: str | None,
    steps: list[dict[str, Any]],
) -> None:
    """Append a success trace; trigger refinement when the threshold is hit.

    `steps` is a list of `{index, action, role, summary}` dicts (same shape
    as the playbook's `PlaybookStep.to_dict()` — caller already produces
    these via `_persist_playbook`).

    Caller invokes this fire-and-forget. We catch all errors so a logging
    failure never breaks the agent.
    """
    if not package or package == "unknown":
        return
    if not _SKILL_DIR.joinpath(f"{package}.md").exists():
        # No baseline handbook for this package — refinement has nothing to
        # build on. Skip silently; user is expected to seed the first
        # version manually.
        return
    try:
        pkg_archive = _ensure_archive(package)
        state = _load_state(pkg_archive)
        entry = {
            "ts": _now(),
            "team_id": team_id,
            "goal_kind": goal_kind,
            "steps": steps,
        }
        _append_success_log(pkg_archive, entry)
        state.successes_since_last_refine += 1
        _save_state(pkg_archive, state)
        log.info(
            "app skill success recorded package=%s kind=%s counter=%d/%d",
            package, goal_kind, state.successes_since_last_refine, state.refine_threshold,
        )
        if state.successes_since_last_refine < state.refine_threshold:
            return
    except Exception as e:  # noqa: BLE001
        log.warning("app skill record_success failed package=%s error=%s", package, e)
        return

    # Threshold hit — fire refinement in the background. Don't await; the
    # triggering caller (a ReAct turn) is done with this work. Hold a
    # reference in a module-scoped set so the task isn't GC'd mid-run.
    task = asyncio.create_task(_refine_safely(package=package, team_id=team_id))
    _PENDING_REFINE_TASKS.add(task)
    task.add_done_callback(_PENDING_REFINE_TASKS.discard)


async def _refine_safely(*, package: str, team_id: int) -> None:
    try:
        # Hold off the LLM call while the provider is already busy enough.
        # Threshold is `settings.react_refine_max_concurrent_agents`:
        #   - 0 means strict idle (single-slot providers like Ollama)
        #   - N means refine may run alongside up to N agent sessions
        if not await _wait_for_capacity(package=package):
            log.info(
                "app skill refine deferred package=%s: provider stayed busy for %ds; "
                "next successful run will retry",
                package, REFINE_WAIT_TOTAL_SEC,
            )
            return
        await _refine_locked(package=package, team_id=team_id)
    except Exception as e:  # noqa: BLE001
        log.exception("app skill refine failed package=%s error=%s", package, e)


async def _wait_for_capacity(*, package: str) -> bool:
    """Wait until the in-flight ReAct count drops to the configured threshold.

    Returns False after `REFINE_WAIT_TOTAL_SEC` of continuous busyness —
    caller treats that as "try again on the next successful run".
    """
    # Late import: `settings` reads config from env at module load; tests can
    # override via monkeypatching and we want to pick that up at call time.
    from app.core.config import settings

    max_allowed = max(0, int(settings.react_refine_max_concurrent_agents))
    deadline = asyncio.get_event_loop().time() + REFINE_WAIT_TOTAL_SEC
    waited = False
    while asyncio.get_event_loop().time() < deadline:
        active = active_agent_sessions()
        if active <= max_allowed:
            if waited:
                log.info(
                    "app skill refine resumed package=%s: active_sessions=%d ≤ %d",
                    package, active, max_allowed,
                )
            return True
        waited = True
        await asyncio.sleep(REFINE_POLL_SEC)
    return False


async def _refine_locked(*, package: str, team_id: int) -> None:
    lock = _PACKAGE_LOCKS.setdefault(package, asyncio.Lock())
    if lock.locked():
        log.info("app skill refine already in progress package=%s; skipping", package)
        return
    async with lock:
        pkg_archive = _ensure_archive(package)
        state = _load_state(pkg_archive)
        if state.successes_since_last_refine < state.refine_threshold:
            # Could have been reset by a concurrent refinement.
            return
        await _do_refine(package=package, team_id=team_id, pkg_archive=pkg_archive, state=state)


async def _do_refine(
    *, package: str, team_id: int, pkg_archive: Path, state: _PackageState
) -> None:
    active_path = _SKILL_DIR / f"{package}.md"
    current_text = active_path.read_text(encoding="utf-8")
    front, body = _split_frontmatter(current_text)
    successes = _read_recent_successes(pkg_archive, SUCCESS_LOG_FOR_REFINEMENT)
    if not successes:
        log.info("app skill refine package=%s: no successes recorded; skipping", package)
        state.successes_since_last_refine = 0
        _save_state(pkg_archive, state)
        return

    log.info(
        "app skill refine start package=%s team=%s feed=%d threshold=%d",
        package, team_id, len(successes), state.refine_threshold,
    )

    new_body = await _ask_llm_to_refine(
        team_id=team_id, package=package, current_body=body, successes=successes
    )
    if not new_body or new_body.strip() == body.strip():
        log.info("app skill refine package=%s: LLM returned unchanged body; skipping", package)
        # Reset counter anyway so we don't loop on the same threshold.
        state.successes_since_last_refine = 0
        _save_state(pkg_archive, state)
        return

    # Archive current → write new active. Both go through `_atomic_write`
    # (tmp + rename) so a crash mid-write never leaves a half-finished
    # archive file that future rollbacks would pick up as garbage.
    state.version_count += 1
    archive_path = pkg_archive / f"{_archive_stamp()}-v{state.version_count}.md"
    _atomic_write(archive_path, current_text)
    new_text = _join_frontmatter(front, new_body.strip())
    _atomic_write(active_path, new_text)

    state.successes_since_last_refine = 0
    state.last_refined_at = _now()
    _save_state(pkg_archive, state)

    # Drop the loader's cache so the next decision picks up the refined doc.
    reload_skills()

    log.info(
        "app skill refine done package=%s version=v%d archive=%s new_chars=%d",
        package, state.version_count, archive_path.name, len(new_text),
    )


async def _ask_llm_to_refine(
    *,
    team_id: int,
    package: str,
    current_body: str,
    successes: list[dict[str, Any]],
) -> str:
    # Imported lazily so import cycles with react_agent stay loose.
    from app.ai.providers import ChatMessage, get_provider
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        provider = await get_provider(db, team_id)

    sys = (
        "你是一名 Android UI 自动化代理的资深教练。给定一份现有的 app 操作手册，"
        "以及一批近期成功的执行轨迹（action + 角色 + 思考摘要），"
        "请基于现有手册产出一份**改进版**，要求：\n"
        "1) 保留原有结构与位置导向的描述方式，**不要写死具体 UI 文字字符串**（如 '发送'、'图片'），"
        "改用结构信号（page 字段、节点 cls/editable、相对位置、子节点构成）；\n"
        "2) 把轨迹中体现的可复用模式以同样的结构语言补充/精炼到手册；\n"
        "3) 删除或合并已被新模式取代的过时段落；\n"
        "4) 控制总长度在 6000 字符以内；\n"
        "5) 只输出新的 markdown 正文（不要包含 frontmatter / `---` 分隔符 / 任何代码块包裹）。"
    )

    successes_render = "\n\n".join(_render_success(s) for s in successes)
    user = (
        f"【目标包名】{package}\n\n"
        f"【现有手册正文】\n{current_body.strip()}\n\n"
        f"【最近 {len(successes)} 条成功轨迹】\n{successes_render}\n\n"
        "请输出改进后的手册正文。"
    )

    result = await provider.chat(
        [ChatMessage(role="system", content=sys), ChatMessage(role="user", content=user)],
        temperature=0.2,
        max_tokens=4096,
    )
    text = (result.text or "").strip()
    # Some models still wrap output in ```markdown ... ``` despite instructions.
    text = _strip_markdown_fence(text)
    if len(text) > MAX_HANDBOOK_CHARS * 2:
        # Hard cap as a safety net — refuse pathological outputs.
        log.warning(
            "app skill refine package=%s: LLM output too large (%d chars), rejecting",
            package, len(text),
        )
        return ""
    return text


def _render_success(entry: dict[str, Any]) -> str:
    kind = entry.get("goal_kind") or "?"
    steps = entry.get("steps") or []
    lines = [f"- 目标类型 {kind}（{entry.get('ts')}）"]
    for s in steps:
        role = s.get("role") or "-"
        summary = (s.get("summary") or "").replace("\n", " ").strip()[:120]
        lines.append(f"    · {s.get('index')}. {s.get('action')}(role={role}) — {summary}")
    return "\n".join(lines)


# ---- storage helpers ------------------------------------------------------


def _ensure_archive(package: str) -> Path:
    pkg = _ARCHIVE_ROOT / package
    pkg.mkdir(parents=True, exist_ok=True)
    return pkg


def _load_state(pkg_archive: Path) -> _PackageState:
    path = pkg_archive / "state.json"
    if not path.exists():
        return _PackageState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _PackageState(
            successes_since_last_refine=int(raw.get("successes_since_last_refine") or 0),
            refine_threshold=int(raw.get("refine_threshold") or DEFAULT_REFINE_THRESHOLD),
            version_count=int(raw.get("version_count") or 0),
            last_refined_at=raw.get("last_refined_at"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("app skill state load failed path=%s error=%s", path, e)
        return _PackageState()


def _save_state(pkg_archive: Path, state: _PackageState) -> None:
    path = pkg_archive / "state.json"
    _atomic_write(path, json.dumps(state.to_dict(), ensure_ascii=False, indent=2))


def _append_success_log(pkg_archive: Path, entry: dict[str, Any]) -> None:
    path = pkg_archive / "success_log.jsonl"
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # Periodically trim to keep things bounded.
    try:
        size_bytes = path.stat().st_size
        if size_bytes > 256 * 1024:  # ~256KB, then check line count
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > SUCCESS_LOG_KEEP:
                kept = lines[-SUCCESS_LOG_KEEP:]
                _atomic_write(path, "\n".join(kept) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _read_recent_successes(pkg_archive: Path, count: int) -> list[dict[str, Any]]:
    path = pkg_archive / "success_log.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-count:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# Frontmatter is only recognised when the file *opens* with at least one
# `key: value` line — any other appearance of `---` mid-document is a
# regular markdown horizontal rule and must not be treated as a separator.
# Without this, a refined handbook body containing `## section\n---\n## next`
# would, on the next refinement pass, get its first section silently
# absorbed into the "frontmatter" half.
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")


def _split_frontmatter(text: str) -> tuple[str, str]:
    # The first non-empty line must look like a `key: value` declaration
    # for the file to be considered as having frontmatter at all.
    stripped = text.lstrip()
    first_line = stripped.split("\n", 1)[0] if stripped else ""
    if not _FRONTMATTER_KEY_RE.match(first_line):
        return "", text
    if _FRONTMATTER_SEP not in text:
        return "", text
    head, _, body = text.partition(_FRONTMATTER_SEP)
    return head + _FRONTMATTER_SEP.rstrip("\n"), body


def _join_frontmatter(front: str, body: str) -> str:
    if not front:
        return body + "\n"
    return f"{front}\n\n{body.strip()}\n"


def _strip_markdown_fence(text: str) -> str:
    # Remove leading/trailing ``` blocks the model sometimes adds.
    m = re.match(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", text, flags=re.DOTALL)
    if m:
        return m.group(1)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _archive_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---- maintenance API ------------------------------------------------------


def rollback(package: str) -> Path | None:
    """Restore the most recent archived version as active. Returns the
    archive file that was promoted, or None if nothing to roll back to.
    Useful when a refinement makes things worse."""
    pkg_archive = _ARCHIVE_ROOT / package
    if not pkg_archive.exists():
        return None
    candidates = sorted(pkg_archive.glob("*.md"))
    if not candidates:
        return None
    latest = candidates[-1]
    target = _SKILL_DIR / f"{package}.md"
    if target.exists():
        bak = pkg_archive / f"{_archive_stamp()}-pre-rollback.md"
        shutil.copy2(target, bak)
    shutil.copy2(latest, target)
    reload_skills()
    log.info("app skill rolled back package=%s from=%s", package, latest.name)
    return latest
