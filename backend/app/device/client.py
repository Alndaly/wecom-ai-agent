from __future__ import annotations

import logging
import time
from typing import Any

from app.core.ws_manager import hub
from app.models import Robot

from .protocol import DeviceCommandName, DeviceCommandResult, UiDump

log = logging.getLogger(__name__)


class DeviceClient:
    """Typed request/response client for Android `device.command` primitives."""

    def __init__(self, robot: Robot) -> None:
        self.robot = robot

    async def command(
        self,
        command: DeviceCommandName,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> DeviceCommandResult:
        started = time.monotonic()
        safe_payload = _safe_payload(payload or {})
        log.info(
            "device command start robot=%s command=%s payload=%s timeout=%.1fs",
            self.robot.robot_id,
            command,
            safe_payload,
            timeout,
        )
        raw = await hub.send_request(
            self.robot.robot_id,
            "device.command",
            {"command": command, **(payload or {})},
            timeout=timeout,
        )
        result = DeviceCommandResult.model_validate(raw)
        elapsed = int((time.monotonic() - started) * 1000)
        log.info(
            "device command result robot=%s command=%s ok=%s msg=%r elapsed=%dms",
            self.robot.robot_id,
            command,
            result.ok,
            result.message,
            elapsed,
        )
        return result

    async def dump_ui(self, *, reason: str = "react", timeout: float = 8.0) -> UiDump:
        started = time.monotonic()
        log.info(
            "device dump_ui start robot=%s reason=%s timeout=%.1fs",
            self.robot.robot_id,
            reason,
            timeout,
        )
        raw = await hub.send_request(
            self.robot.robot_id,
            "device.command",
            {"command": "dump_ui", "reason": reason},
            timeout=timeout,
        )
        dump = UiDump.model_validate(raw)
        elapsed = int((time.monotonic() - started) * 1000)
        log.info(
            "device dump_ui result robot=%s nodes=%d page=%s elapsed=%dms",
            self.robot.robot_id,
            len(dump.nodes),
            dump.current_page,
            elapsed,
        )
        return dump

    async def open_wecom(self, *, timeout: float = 6.0) -> DeviceCommandResult:
        return await self.command("open_wecom", timeout=timeout)

    async def screenshot_once(self, *, timeout: float = 10.0) -> DeviceCommandResult:
        return await self.command("screenshot_once", timeout=timeout)

    async def tap_text(self, text: str, *, timeout: float) -> DeviceCommandResult:
        return await self.command("tap_text", {"text": text}, timeout=timeout)

    async def tap_xy(self, x: int, y: int, *, timeout: float) -> DeviceCommandResult:
        return await self.command("tap_xy", {"x": x, "y": y}, timeout=timeout)

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 280,
        timeout: float,
    ) -> DeviceCommandResult:
        return await self.command(
            "swipe",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms},
            timeout=timeout,
        )

    async def input_text(self, text: str, *, timeout: float) -> DeviceCommandResult:
        return await self.command("input_text", {"text": text}, timeout=timeout)

    async def back(self, *, timeout: float) -> DeviceCommandResult:
        return await self.command("back", timeout=timeout)

    async def home(self, *, timeout: float) -> DeviceCommandResult:
        return await self.command("home", timeout=timeout)


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "text" in out:
        text = str(out["text"])
        out["text"] = f"<text len={len(text)} preview={text[:24]!r}>"
    return out
