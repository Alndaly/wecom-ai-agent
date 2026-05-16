from app.ai.react_agent import AgentResult, _guard_decision
from app.ai.react_agent import AgentStep, _Observation, _post_back_verdict, _post_tap_verdict, _stuck_repeating
from app.ai.react_agent import _swipe_coords
from app.device import UiNode


def _bounds(screen: tuple[int, int], left: float, top: float, right: float, bottom: float) -> list[int]:
    width, height = screen
    return [int(width * left), int(height * top), int(width * right), int(height * bottom)]


def _observation(
    *,
    package: str = "example.app",
    page: str = "surface",
    screen: tuple[int, int] = (1000, 2000),
    nodes: list[UiNode] | None = None,
    input_panel_visible: bool | None = None,
) -> _Observation:
    node_map = {node.id: node for node in nodes or []}
    return _Observation(
        tree=f"=== UI dump pkg={package} page={page} ===",
        nodes=node_map,
        screen_size=screen,
        input_panel_visible=input_panel_visible,
    )


def _editable_node(node_id: int, text: str = "", screen: tuple[int, int] = (1000, 2000)) -> UiNode:
    return UiNode(
        id=node_id,
        cls="android.widget.EditText",
        text=text,
        editable=True,
        focusable=True,
        bounds=_bounds(screen, 0.1, 0.9, 0.8, 0.98),
    )


def _button_node(node_id: int, text: str = "", screen: tuple[int, int] = (1000, 2000)) -> UiNode:
    return UiNode(
        id=node_id,
        cls="android.widget.Button",
        text=text,
        clickable=True,
        bounds=_bounds(screen, 0.82, 0.9, 0.98, 0.98),
    )


def test_post_back_verdict_detects_input_panel_dismissed_without_goal_match() -> None:
    step = AgentStep(1, "", "back", {}, True, "已返回 (before_pkg=before.app before_page=form before_input_panel=true)", 1)

    verdict = _post_back_verdict(
        step,
        _observation(package="before.app", page="form", nodes=[_editable_node(1)], input_panel_visible=False),
    )

    assert verdict is not None
    assert "输入面板已消失" in verdict


def test_guard_blocks_back_after_context_changed_without_goal_match() -> None:
    steps = [
        AgentStep(
            1,
            "",
            "back",
            {},
            True,
            "已返回 (before_pkg=before.app before_page=form before_input_panel=false) [验证] 上下文从 pkg=before.app 变为 pkg=after.app",
            1,
        )
    ]

    result = _guard_decision({"action": "back", "args": {}}, _observation(package="after.app", page="surface"), steps)

    assert isinstance(result, AgentResult)
    assert result.ok is False


def test_guard_stops_repeated_back_only_when_page_actually_changed() -> None:
    # Android BACK has two stages: dismiss keyboard first, then leave page.
    # "panel collapsed AND page changed" is real navigation → declare done.
    steps_real_nav = [
        AgentStep(
            1,
            "",
            "back",
            {},
            True,
            "已返回 (before_pkg=before.app before_page=form before_input_panel=true) "
            "[验证] 输入面板已消失；上下文从 page=form 变为 home",
            1,
        )
    ]
    result = _guard_decision(
        {"action": "back", "args": {}},
        _observation(package="before.app", page="home"),
        steps_real_nav,
    )
    assert isinstance(result, AgentResult)
    assert result.ok is True


def test_guard_allows_second_back_when_panel_only_collapsed() -> None:
    # Previous back only dismissed the keyboard (panel: true → false) but the
    # page didn't change — we're still on the same screen. The next back must
    # be allowed through so it can actually leave the page.
    steps_panel_only = [
        AgentStep(
            1,
            "",
            "back",
            {},
            True,
            "已返回 (before_pkg=before.app before_page=form before_input_panel=true) "
            "[验证] 输入面板已消失",
            1,
        )
    ]
    result = _guard_decision(
        {"action": "back", "args": {}},
        _observation(package="before.app", page="form"),
        steps_panel_only,
    )
    assert result is None  # guard does not short-circuit; back action proceeds


def test_post_tap_verdict_detects_send_from_executed_message_without_locator_role() -> None:
    screen = (1000, 2000)
    send_node_id = 2
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": send_node_id},
        ok=True,
        message=f"tap_node({send_node_id} label='发送') -> clicked",
        elapsed_ms=87,
    )

    verdict = _post_tap_verdict(
        step,
        _observation(
            screen=screen,
            nodes=[
                _editable_node(1, "请问您还有其他需要我协助了解的知识点", screen),
                _button_node(send_node_id, "发送", screen),
            ],
        ),
        "把输入框中的消息发送出去",
    )

    assert verdict is not None
    assert "仍有文本" in verdict


def test_stuck_repeating_ignores_locator_role_when_same_node_keeps_failing() -> None:
    node_id = 2
    steps = [
        AgentStep(
            i,
            "",
            "tap_node",
            args,
            True,
            f"tap_node({node_id} label='发送') [验证] 输入框仍有文本",
            1,
        )
        for i, args in enumerate(
            [
                {"node_id": node_id},
                {"node_id": node_id, "_locator_role": "send_button"},
                {"node_id": node_id},
            ],
            start=1,
        )
    ]

    assert _stuck_repeating(steps, n=3) is True


def test_swipe_coords_scale_with_observed_screen_size() -> None:
    small_screen = (720, 1280)
    large_screen = (small_screen[0] * 2, small_screen[1] * 2)
    small = _observation(screen=small_screen)
    large = _observation(screen=large_screen)

    def expected_up(screen: tuple[int, int]) -> tuple[int, int, int, int]:
        width, height = screen
        top = int(height * 0.15)
        bottom = int(height * 0.85)
        center_x = width // 2
        center_y = (top + bottom) // 2
        delta_y = (bottom - top) // 3
        return center_x, center_y + delta_y, center_x, center_y - delta_y

    assert _swipe_coords("up", small, None) == expected_up(small_screen)
    assert _swipe_coords("up", large, None) == expected_up(large_screen)


def test_swipe_coords_fall_back_to_observed_node_bounds_when_screen_size_missing() -> None:
    source_screen = (720, 1280)
    obs = _observation(
        screen=(0, 0),
        nodes=[
            UiNode(id=1, cls="android.widget.TextView", bounds=_bounds(source_screen, 0.03, 0.08, 0.97, 0.24)),
            UiNode(
                id=2,
                cls="androidx.recyclerview.widget.RecyclerView",
                scrollable=True,
                bounds=_bounds(source_screen, 0.0, 0.27, 1.0, 0.92),
            ),
        ],
    )

    observed_bounds = [node.bounds for node in obs.nodes.values()]
    left = min(bounds[0] for bounds in observed_bounds)
    top = min(bounds[1] for bounds in observed_bounds)
    right = max(bounds[2] for bounds in observed_bounds)
    bottom = max(bounds[3] for bounds in observed_bounds)
    region_top = top + int((bottom - top) * 0.15)
    region_bottom = top + int((bottom - top) * 0.85)
    center_x = (left + right) // 2
    center_y = (region_top + region_bottom) // 2
    delta_y = (region_bottom - region_top) // 3
    assert _swipe_coords("down", obs, None) == (center_x, center_y - delta_y, center_x, center_y + delta_y)
