from __future__ import annotations

from typing import Any

from app.core.ws_manager import hub
from app.models import Robot

from .protocol import DeviceCommandName, DeviceCommandResult, UiDump


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
        raw = await hub.send_request(
            self.robot.robot_id,
            "device.command",
            {"command": command, **(payload or {})},
            timeout=timeout,
        )
        return DeviceCommandResult.model_validate(raw)

    async def dump_ui(self, *, reason: str = "react", timeout: float = 8.0) -> UiDump:
        raw = await hub.send_request(
            self.robot.robot_id,
            "device.command",
            {"command": "dump_ui", "reason": reason},
            timeout=timeout,
        )
        return UiDump.model_validate(raw)

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
