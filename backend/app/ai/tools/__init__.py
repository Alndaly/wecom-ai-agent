"""Tool registry for the conversational ReAct agent.

A `Tool` is anything the agent can call: built-in helpers (kb_search,
escalate, ...), user-defined skills loaded from `skills/*.py`, or MCP-server
tools loaded at startup. They share one schema so the agent's prompt only has
to render one table.

The registry is a singleton — built once at app startup, reset only in tests.
Tools added at runtime (e.g. when MCP servers reconnect) call `register()`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Per-invocation context passed to every tool. Plumbing things this way
    keeps tools pure-async functions without surprise DI from globals."""
    db: AsyncSession
    team_id: int
    conversation_id: int
    contact_id: int
    inbound_text: str
    trace_id: str
    # Outputs the agent reads after each call
    scratch: dict[str, Any] = field(default_factory=dict)


ToolFn = Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    # JSON-schema-ish: { "param_name": {"type": "string", "desc": "..."} }
    params: dict[str, dict[str, str]]
    call: ToolFn
    source: str = "builtin"  # builtin | skill | mcp:<server_name>

    def render_for_prompt(self) -> str:
        if self.params:
            args = ", ".join(
                f"{k}:{v.get('type', 'any')}" for k, v in self.params.items()
            )
        else:
            args = ""
        return f"- {self.name}({args}) — {self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            existing = self._tools[tool.name]
            log.warning(
                "tool name collision: %s (existing source=%s, new=%s) — overwriting",
                tool.name, existing.source, tool.source,
            )
        self._tools[tool.name] = tool
        log.info("tool registered: %s (%s)", tool.name, tool.source)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def render_catalog(self) -> str:
        return "\n".join(t.render_for_prompt() for t in self._tools.values())


_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _registry


def reset_registry_for_tests() -> None:
    global _registry
    _registry = ToolRegistry()
