"""Persona system — pluggable "soul / memory / style" markdown for conv_agent.

Inspired by the OpenClaude / Anthropic-style internal prompts that split an
agent's identity into separate concerns (who I am, how I remember, how I
speak). Each persona lives in its own directory under `app/ai/personas/`;
each file is a single concern and can be edited independently without
touching code.

Layout:

    app/ai/personas/
    └── default/
        ├── manifest.md       ← metadata (id, name, description); not injected
        ├── soul.md           ← identity / boundaries
        ├── memory.md         ← how to use profile + history
        └── style.md          ← speaking style + anti-AI tells

The loader concatenates the three injected files in a fixed order
(`soul → memory → style`) into a single system-prompt block.

The active persona is picked by the team's `ai.persona_id` setting; if a
team hasn't chosen one or the chosen one doesn't exist, we fall back to
`default`. Add a new persona by dropping a directory next to `default/` —
no code changes needed.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


class PersonaError(Exception):
    """Raised for any persona-CRUD problem the API should surface as 4xx."""


# Slug rules: lowercase ASCII letters, digits, dashes/underscores. Min 1
# char, max 64. No leading dot (would be hidden), no path separators —
# both for tidiness and to defeat any path-traversal attempt that could
# escape `personas/`.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# `default` is the universal fallback; deleting it would leave runs with
# unknown persona_id with no answer.
_PROTECTED_IDS: frozenset[str] = frozenset({"default"})

_PERSONA_DIR = Path(__file__).with_name("personas")
# Files injected into the prompt, in this exact order. Anything else in the
# persona directory (manifest, README, etc.) is metadata only.
_PERSONA_SECTIONS: tuple[str, ...] = ("soul.md", "memory.md", "style.md")
_FRONTMATTER_SEP = "\n---\n"
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")


@dataclass(frozen=True)
class Persona:
    """One persona — its id (=dir name), display metadata, and the
    composed `system_block` that will be inlined into conv_agent's
    system prompt."""

    id: str
    name: str
    description: str
    system_block: str

    def is_empty(self) -> bool:
        return not self.system_block.strip()


@lru_cache(maxsize=1)
def _index() -> dict[str, Persona]:
    if not _PERSONA_DIR.exists():
        return {}
    out: dict[str, Persona] = {}
    for child in sorted(_PERSONA_DIR.iterdir()):
        if not child.is_dir():
            continue
        try:
            persona = _load_persona(child)
        except Exception as e:  # noqa: BLE001
            log.warning("persona load failed dir=%s error=%s", child, e)
            continue
        if persona.is_empty():
            log.warning(
                "persona dir=%s has no injected sections (%s) — skipping",
                child.name, ", ".join(_PERSONA_SECTIONS),
            )
            continue
        out[persona.id] = persona
        log.info(
            "persona loaded id=%s name=%s chars=%d",
            persona.id, persona.name, len(persona.system_block),
        )
    return out


def _load_persona(dir_path: Path) -> Persona:
    meta = _read_manifest(dir_path)
    sections: list[str] = []
    for fname in _PERSONA_SECTIONS:
        path = dir_path / fname
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8").strip()
        if body:
            sections.append(body)
    system_block = "\n\n".join(sections)
    return Persona(
        id=meta.get("id") or dir_path.name,
        name=meta.get("name") or dir_path.name,
        description=meta.get("description", ""),
        system_block=system_block,
    )


def _read_manifest(dir_path: Path) -> dict[str, str]:
    """Parse `manifest.md`'s frontmatter (if present). Frontmatter must
    open the file with at least one `key: value` line and end with `\\n---\\n`;
    anything else is treated as no manifest (and id/name fall back to the
    directory name)."""
    path = dir_path / "manifest.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return {}
    first_line = stripped.split("\n", 1)[0]
    if not _FRONTMATTER_KEY_RE.match(first_line) or _FRONTMATTER_SEP not in text:
        return {}
    head, _, _body = text.partition(_FRONTMATTER_SEP)
    out: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k and v:
            out[k] = v
    return out


def get_persona(persona_id: str | None) -> Persona | None:
    """Return the persona matching `persona_id`, or the `default` persona
    if the id is empty/unknown, or None when no personas are registered
    at all (caller should handle gracefully)."""
    idx = _index()
    if not idx:
        return None
    key = (persona_id or "").strip() or "default"
    persona = idx.get(key)
    if persona is None and key != "default":
        log.info("persona id=%s not found; falling back to default", key)
        persona = idx.get("default")
    return persona


def all_personas() -> list[Persona]:
    return list(_index().values())


def reload_personas() -> None:
    """Drop the loader cache. Tests + dev hot-edit call this."""
    _index.cache_clear()


# ---- CRUD ---------------------------------------------------------------
# All write paths funnel through the same id-validation + path-confinement
# logic. The web API exposes these directly; admins can also call them from
# a shell. Reads pop the cache so the next conv_agent decision sees the
# update without a server restart.


@dataclass(frozen=True)
class PersonaDetail:
    """Full persona contents — what the editor UI consumes."""

    id: str
    name: str
    description: str
    soul: str
    memory: str
    style: str


def list_personas() -> list[dict[str, str]]:
    """Brief listing for the picker UI: id, name, description, char count."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "chars": str(len(p.system_block)),
            "protected": "true" if p.id in _PROTECTED_IDS else "false",
        }
        for p in all_personas()
    ]


def get_persona_detail(persona_id: str) -> PersonaDetail | None:
    """Return all four sections for a single persona, or None if missing."""
    safe_id = _validated_id(persona_id)
    pdir = _PERSONA_DIR / safe_id
    if not pdir.is_dir():
        return None
    meta = _read_manifest(pdir)
    return PersonaDetail(
        id=safe_id,
        name=meta.get("name") or safe_id,
        description=meta.get("description", ""),
        soul=_read_section(pdir, "soul.md"),
        memory=_read_section(pdir, "memory.md"),
        style=_read_section(pdir, "style.md"),
    )


def create_persona(
    *,
    persona_id: str,
    name: str,
    description: str = "",
    soul: str = "",
    memory: str = "",
    style: str = "",
) -> PersonaDetail:
    """Create a new persona directory + the 4 files. Raises if id exists."""
    safe_id = _validated_id(persona_id)
    pdir = _PERSONA_DIR / safe_id
    if pdir.exists():
        raise PersonaError(f"persona id already exists: {safe_id}")
    pdir.mkdir(parents=True)
    try:
        _write_manifest(pdir, safe_id, name=name, description=description)
        _write_section(pdir, "soul.md", soul)
        _write_section(pdir, "memory.md", memory)
        _write_section(pdir, "style.md", style)
    except Exception:
        # Clean up the half-built directory so a retry isn't blocked by
        # the "already exists" check above.
        shutil.rmtree(pdir, ignore_errors=True)
        raise
    reload_personas()
    log.info("persona created id=%s name=%s", safe_id, name)
    detail = get_persona_detail(safe_id)
    assert detail is not None
    return detail


def update_persona(
    *,
    persona_id: str,
    name: str | None = None,
    description: str | None = None,
    soul: str | None = None,
    memory: str | None = None,
    style: str | None = None,
) -> PersonaDetail:
    """Patch one or more sections. None means "leave alone"."""
    safe_id = _validated_id(persona_id)
    pdir = _PERSONA_DIR / safe_id
    if not pdir.is_dir():
        raise PersonaError(f"persona not found: {safe_id}")
    if name is not None or description is not None:
        current = _read_manifest(pdir)
        _write_manifest(
            pdir,
            safe_id,
            name=name if name is not None else current.get("name", safe_id),
            description=(
                description if description is not None else current.get("description", "")
            ),
        )
    if soul is not None:
        _write_section(pdir, "soul.md", soul)
    if memory is not None:
        _write_section(pdir, "memory.md", memory)
    if style is not None:
        _write_section(pdir, "style.md", style)
    reload_personas()
    log.info("persona updated id=%s", safe_id)
    detail = get_persona_detail(safe_id)
    assert detail is not None
    return detail


def delete_persona(persona_id: str) -> None:
    """Remove the persona directory entirely. Refuses to delete protected ids."""
    safe_id = _validated_id(persona_id)
    if safe_id in _PROTECTED_IDS:
        raise PersonaError(f"persona '{safe_id}' is protected and can't be deleted")
    pdir = _PERSONA_DIR / safe_id
    if not pdir.is_dir():
        raise PersonaError(f"persona not found: {safe_id}")
    shutil.rmtree(pdir)
    reload_personas()
    log.info("persona deleted id=%s", safe_id)


def _validated_id(persona_id: str) -> str:
    """Reject anything that's not a safe slug. This is the single chokepoint
    for path-traversal hygiene — every CRUD function goes through it before
    touching the filesystem."""
    pid = (persona_id or "").strip().lower()
    if not _ID_RE.match(pid):
        raise PersonaError(
            "persona id must match [a-z0-9][a-z0-9_-]{0,63} "
            "(lowercase letters/digits, dashes/underscores; no slashes)"
        )
    return pid


def _read_section(dir_path: Path, filename: str) -> str:
    path = dir_path / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_section(dir_path: Path, filename: str, content: str) -> None:
    path = dir_path / filename
    # Trailing newline so terminal git diffs render cleanly.
    text = content if content.endswith("\n") else content + "\n"
    _atomic_write(path, text)


def _write_manifest(
    dir_path: Path, persona_id: str, *, name: str, description: str
) -> None:
    safe_name = (name or persona_id).strip().replace("\n", " ")
    safe_desc = (description or "").strip().replace("\n", " ")
    text = (
        f"id: {persona_id}\n"
        f"name: {safe_name}\n"
        f"description: {safe_desc}\n"
        f"version: 1\n"
        f"---\n"
    )
    _atomic_write(dir_path / "manifest.md", text)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
