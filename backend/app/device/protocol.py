from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UiNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    cls: str = ""
    view_id: str = ""
    text: str = ""
    desc: str = ""
    clickable: bool = False
    focusable: bool = False
    editable: bool = False
    scrollable: bool = False
    bounds: list[int] = Field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        if len(self.bounds) != 4:
            return (0, 0)
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2


class UiDump(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str | None = None
    robot_id: str | None = None
    current_page: str | None = None
    reason: str = ""
    tree: str = ""
    nodes: list[UiNode] = Field(default_factory=list)
    screen_width: int | None = None
    screen_height: int | None = None
    input_panel_visible: bool | None = None
    path: str | None = None
    created_at: str | None = None


class DeviceCommandResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str
    request_id: str | None = None
    ok: bool = False
    message: str | None = None
    data: dict[str, Any] | None = None


DeviceCommandName = Literal[
    "dump_ui",
    "screenshot_once",
    "react_session_start",
    "react_session_end",
    "tap_text",
    "tap_node",
    "tap_xy",
    "double_tap_node",
    "double_tap_xy",
    "long_press_node",
    "long_press_xy",
    "drag_xy",
    "swipe",
    "input_text",
    "stage_media",
    "back",
    "home",
    "open_wecom",
]
