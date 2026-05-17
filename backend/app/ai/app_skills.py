"""App-operation skills — per-application playbooks for the ReAct agent.

Each skill is a markdown file in `app_skills/` describing how to operate one
Android app: page taxonomy, layout patterns, common recovery moves. Skills
are keyed by Android package name so the agent can pick the right manual
based on the device's current foreground app (UI tree first line carries
`pkg=...`).

Disambiguation: there is also `app/ai/tools/skills.py` for LLM-callable
function tools. Different concept entirely — that one registers callables
the LLM invokes; this one is reference documentation injected into the
LLM's user prompt.

File format (frontmatter optional, separated by a line of `---`):

    package: com.tencent.wework
    name: 企业微信
    description: 聊天/相册/搜索等场景的操作手册
    ---

    # markdown body...

If frontmatter is missing the filename stem is used as both package and
name. Empty fields fall back to the package id.

Adding a new app skill: drop a `.md` in `app_skills/`. No code changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_SKILL_DIR = Path(__file__).with_name("app_skills")
_FRONTMATTER_SEP = "\n---\n"


@dataclass(frozen=True)
class AppSkill:
    package: str
    name: str
    description: str
    body: str

    def as_prompt_block(self, header_label: str = "操作手册") -> str:
        """Format for inclusion in the LLM user message."""
        title = f"【{self.name}（{self.package}）{header_label}】"
        return f"{title}\n{self.body}"


@lru_cache(maxsize=1)
def _index() -> dict[str, AppSkill]:
    if not _SKILL_DIR.exists():
        return {}
    skills: dict[str, AppSkill] = {}
    for path in sorted(_SKILL_DIR.glob("*.md")):
        try:
            skill = _parse(path)
        except Exception as e:  # noqa: BLE001
            log.warning("app skill load failed path=%s error=%s", path, e)
            continue
        if skill.package in skills:
            log.warning(
                "app skill duplicate package=%s files=%s and %s; keeping later",
                skill.package, skills[skill.package].package, path.name,
            )
        skills[skill.package] = skill
        log.info(
            "app skill loaded package=%s name=%s body_chars=%d",
            skill.package, skill.name, len(skill.body),
        )
    return skills


def _parse(path: Path) -> AppSkill:
    text = path.read_text(encoding="utf-8")
    if _FRONTMATTER_SEP in text:
        head, _, body = text.partition(_FRONTMATTER_SEP)
        meta = _parse_kv(head)
    else:
        # No frontmatter — body is the whole file, derive package from name.
        meta, body = {}, text
    package = meta.get("package") or path.stem
    name = meta.get("name") or package
    return AppSkill(
        package=package,
        name=name,
        description=meta.get("description", ""),
        body=body.strip(),
    )


def _parse_kv(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def skill_for_package(package: str | None) -> AppSkill | None:
    """Return the skill registered for `package`, or None if unknown."""
    if not package or package == "unknown":
        return None
    return _index().get(package)


def all_skills() -> list[AppSkill]:
    return list(_index().values())


def reload_skills() -> None:
    """Force-reload from disk. Useful in tests or for hot-edits during dev."""
    _index.cache_clear()
