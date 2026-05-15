"""Skill loader — pick up user-defined tools from `skills/*.py`.

Each skill file must define a module-level `tool: Tool` (or `tools: list[Tool]`).
Example `skills/weather.py`:

    from app.ai.tools import Tool, ToolContext

    async def _call(ctx: ToolContext, args: dict):
        city = args["city"]
        return f"{city} 现在晴 25°C"  # ← in real life, hit an API

    tool = Tool(
        name="weather",
        description="查询某城市当前天气",
        params={"city": {"type": "string", "desc": "城市中文名"}},
        call=_call,
    )

Loader is best-effort: a broken file is logged and skipped — it never blocks
app startup.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from . import Tool, get_registry

log = logging.getLogger(__name__)


def load_skills_from_dir(directory: str | Path) -> int:
    """Import every `*.py` in `directory` (non-recursive) and register any
    `tool` / `tools` it exposes. Returns the count of tools registered."""
    d = Path(directory)
    if not d.is_dir():
        log.info("skills dir not found: %s — skipping", d)
        return 0

    reg = get_registry()
    count = 0
    for fp in sorted(d.glob("*.py")):
        if fp.name.startswith("_"):
            continue
        try:
            mod_name = f"_skills_{fp.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, fp)
            if spec is None or spec.loader is None:
                log.warning("skill %s: cannot build spec", fp)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            exports: list[Tool] = []
            if hasattr(module, "tool") and isinstance(module.tool, Tool):
                exports.append(module.tool)
            if hasattr(module, "tools"):
                for t in module.tools:
                    if isinstance(t, Tool):
                        exports.append(t)

            if not exports:
                log.warning("skill %s: no `tool` or `tools` found", fp)
                continue
            for t in exports:
                t.source = f"skill:{fp.stem}"
                reg.register(t)
                count += 1
        except Exception as e:  # noqa: BLE001
            log.exception("skill %s failed to load: %s", fp, e)
    log.info("loaded %d skill tool(s) from %s", count, d)
    return count
