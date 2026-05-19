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

    async def react_session_start(self, *, timeout: float = 4.0) -> DeviceCommandResult:
        return await self.command("react_session_start", timeout=timeout)

    async def react_session_end(self, *, timeout: float = 4.0) -> DeviceCommandResult:
        return await self.command("react_session_end", timeout=timeout)

    async def harvest_current_chat(
        self,
        *,
        max_messages: int,
        quiet_window_ms: int = 1200,
        max_duration_ms: int = 10000,
        timeout: float = 16.0,
    ) -> DeviceCommandResult:
        return await self.command(
            "harvest_current_chat",
            {
                "max_messages": max(1, min(int(max_messages), 30)),
                "quiet_window_ms": max(500, min(int(quiet_window_ms), 5000)),
                "max_duration_ms": max(2000, min(int(max_duration_ms), 20000)),
            },
            timeout=timeout,
        )

    async def tap_text(self, text: str, *, timeout: float) -> DeviceCommandResult:
        return await self.command("tap_text", {"text": text}, timeout=timeout)

    async def tap_node(
        self,
        node_id: int,
        x: int,
        y: int,
        *,
        expected: dict[str, Any] | None = None,
        timeout: float,
    ) -> DeviceCommandResult:
        payload = {"node_id": node_id, "x": x, "y": y}
        if expected:
            payload.update(_expected_payload(expected))
        return await self.command("tap_node", payload, timeout=timeout)

    async def tap_xy(self, x: int, y: int, *, timeout: float) -> DeviceCommandResult:
        return await self.command("tap_xy", {"x": x, "y": y}, timeout=timeout)

    async def double_tap_node(
        self,
        node_id: int,
        x: int,
        y: int,
        *,
        expected: dict[str, Any] | None = None,
        timeout: float,
    ) -> DeviceCommandResult:
        payload = {"node_id": node_id, "x": x, "y": y}
        if expected:
            payload.update(_expected_payload(expected))
        return await self.command("double_tap_node", payload, timeout=timeout)

    async def double_tap_xy(self, x: int, y: int, *, timeout: float) -> DeviceCommandResult:
        return await self.command("double_tap_xy", {"x": x, "y": y}, timeout=timeout)

    async def long_press_node(
        self,
        node_id: int,
        x: int,
        y: int,
        *,
        expected: dict[str, Any] | None = None,
        duration_ms: int = 650,
        timeout: float,
    ) -> DeviceCommandResult:
        payload = {"node_id": node_id, "x": x, "y": y, "duration_ms": duration_ms}
        if expected:
            payload.update(_expected_payload(expected))
        return await self.command(
            "long_press_node",
            payload,
            timeout=timeout,
        )

    async def long_press_xy(
        self,
        x: int,
        y: int,
        *,
        duration_ms: int = 650,
        timeout: float,
    ) -> DeviceCommandResult:
        return await self.command(
            "long_press_xy",
            {"x": x, "y": y, "duration_ms": duration_ms},
            timeout=timeout,
        )

    async def drag_xy(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 450,
        timeout: float,
    ) -> DeviceCommandResult:
        return await self.command(
            "drag_xy",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms},
            timeout=timeout,
        )

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

    async def input_text(
        self,
        text: str,
        *,
        node_id: int | None = None,
        expected: dict[str, Any] | None = None,
        mode: str = "replace",
        timeout: float,
    ) -> DeviceCommandResult:
        payload: dict[str, Any] = {"text": text, "mode": mode}
        if node_id is not None:
            payload["node_id"] = node_id
        if expected:
            payload.update(_expected_payload(expected))
        return await self.command("input_text", payload, timeout=timeout)

    async def stage_media(
        self,
        *,
        download_url: str,
        mime: str,
        filename: str,
        timeout: float = 45.0,
    ) -> DeviceCommandResult:
        """Drop a media file into the device's gallery (Pictures/WeComAgent/).

        The subsequent ReAct phase walks "+ → 图片 → 选最新一张 → 发送" to
        actually deliver the message. The result `data` carries `uri`,
        `display_name`, `taken_at_ms`, and `relative_path` so the agent can
        identify the staged file.
        """
        return await self.command(
            "stage_media",
            {
                "download_url": download_url,
                "mime": mime,
                "filename": filename,
            },
            timeout=timeout,
        )

    async def back(self, *, timeout: float) -> DeviceCommandResult:
        return await self.command("back", timeout=timeout)

    async def home(self, *, timeout: float) -> DeviceCommandResult:
        return await self.command("home", timeout=timeout)


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return dict(payload)


def _expected_payload(expected: dict[str, Any]) -> dict[str, Any]:
    return {f"expected_{k}": v for k, v in expected.items()}
