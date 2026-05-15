"""MCP (Model Context Protocol) adapter — exposes MCP server tools as Tools.

Config shape (set via `mcp_servers_json` env var or settings UI):

    [
      {"name": "weather", "command": "uvx", "args": ["mcp-server-weather"]},
      {"name": "fs",      "command": "npx",
       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}
    ]

This module *softly* depends on the `mcp` Python SDK. If it isn't installed
we log a warning and skip — the rest of the agent keeps working with built-in
tools + file skills.

The adapter is intentionally simple: connect at startup, enumerate tools, hold
one stdio session per server for the lifetime of the process. A reconnect /
restart loop can be added in a follow-up.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from . import Tool, ToolContext, get_registry

log = logging.getLogger(__name__)

# (server_name -> session). Held at module scope so the agent can call tools
# without re-doing handshake; ClientSession objects are not thread-safe but
# the agent loop is single-task per request.
_sessions: dict[str, Any] = {}
_exit_stack: AsyncExitStack | None = None


def parse_servers(raw: str) -> list[dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.error("mcp_servers_json is not valid JSON: %s", e)
        return []
    if not isinstance(data, list):
        log.error("mcp_servers_json must be a JSON array, got %s", type(data).__name__)
        return []
    return [s for s in data if isinstance(s, dict) and s.get("name") and s.get("command")]


async def connect_servers(servers: list[dict[str, Any]]) -> int:
    """Connect to each MCP server, enumerate its tools, register them.

    Returns the number of tools registered. Failures per-server are logged and
    don't abort the process.
    """
    global _exit_stack
    if not servers:
        return 0
    try:
        # Soft import — if the user hasn't installed `mcp`, we no-op.
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
    except ImportError:
        log.warning(
            "mcp package not installed; %d MCP server(s) configured but unreachable. "
            "Run `pip install mcp` to enable.",
            len(servers),
        )
        return 0

    if _exit_stack is None:
        _exit_stack = AsyncExitStack()

    reg = get_registry()
    total = 0
    for spec in servers:
        name = spec["name"]
        try:
            params = StdioServerParameters(
                command=spec["command"],
                args=list(spec.get("args") or []),
                env=spec.get("env"),
            )
            read, write = await _exit_stack.enter_async_context(stdio_client(params))
            session = await _exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_resp = await session.list_tools()
            _sessions[name] = session

            for t in tools_resp.tools:
                tool = _wrap_mcp_tool(server_name=name, mcp_tool=t)
                reg.register(tool)
                total += 1
            log.info("mcp server %r connected, %d tool(s)", name, len(tools_resp.tools))
        except Exception as e:  # noqa: BLE001
            log.exception("mcp server %r failed to start: %s", name, e)
    return total


def _wrap_mcp_tool(*, server_name: str, mcp_tool: Any) -> Tool:
    """Adapt an MCP `Tool` (from `session.list_tools()`) to our local `Tool`."""
    # MCP tool name is unique per server; namespace it to avoid collisions.
    local_name = f"{server_name}_{mcp_tool.name}"
    params: dict[str, dict[str, str]] = {}
    schema = getattr(mcp_tool, "inputSchema", None) or {}
    props = (schema.get("properties") if isinstance(schema, dict) else None) or {}
    for k, v in props.items():
        params[k] = {
            "type": str(v.get("type", "any")) if isinstance(v, dict) else "any",
            "desc": str(v.get("description", "")) if isinstance(v, dict) else "",
        }

    async def _call(ctx: ToolContext, args: dict[str, Any]) -> str:
        session = _sessions.get(server_name)
        if session is None:
            return f"ERROR: MCP server {server_name!r} not connected"
        try:
            result = await session.call_tool(mcp_tool.name, args)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: MCP call failed: {e}"
        # Result.content is a list of (TextContent | ImageContent | ...). We
        # collapse text content into a string; images are dropped with a note.
        parts: list[str] = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
            else:
                parts.append(f"[{type(c).__name__} omitted]")
        return "\n".join(parts) if parts else "(empty)"

    return Tool(
        name=local_name,
        description=(getattr(mcp_tool, "description", "") or f"MCP tool {mcp_tool.name}"),
        params=params,
        call=_call,
        source=f"mcp:{server_name}",
    )


async def shutdown() -> None:
    """Close every active MCP session (call at process exit)."""
    global _exit_stack
    if _exit_stack is not None:
        try:
            await _exit_stack.aclose()
        except Exception:  # noqa: BLE001
            log.exception("error closing MCP sessions")
        _exit_stack = None
        _sessions.clear()
