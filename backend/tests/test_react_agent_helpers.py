from app.ai.react_agent import AgentResult, _guard_decision
from app.ai.react_agent import AgentStep, _Observation, _post_back_verdict, _post_tap_verdict, _stuck_repeating
from app.ai.react_agent import _degraded_wecom_observation, _find_sent_message_echo, _node_expectation, _swipe_coords
from app.ai import react_locators
from app.ai.react_locators import LocatorStore
from app.device import UiNode
from app.models import Robot


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


def _robot(
    *,
    robot_id: str = "robot_test",
    model: str = "Pixel 8",
    screen: tuple[int, int] = (1000, 2000),
) -> Robot:
    return Robot(
        id=1,
        team_id=1,
        name="test robot",
        robot_id=robot_id,
        token_hash="hash",
        device_type="android",
        manufacturer="Google",
        model=model,
        android_version="15",
        sdk_int=35,
        app_version="1.0.0",
        screen_width=screen[0],
        screen_height=screen[1],
    )


def _text_node(
    node_id: int,
    text: str,
    bounds: list[int],
    *,
    cls: str = "android.widget.TextView",
) -> UiNode:
    return UiNode(id=node_id, cls=cls, text=text, bounds=bounds)


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


def test_post_tap_verdict_prefers_visible_sent_bubble_over_input_placeholder() -> None:
    screen = (1000, 2000)
    sent_text = "您好，请问有什么需要帮助？"
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": 2, "_locator_role": "send_button"},
        ok=True,
        message="tap_node(2 label='发送') -> clicked",
        elapsed_ms=87,
        obs_meta={"sent_echo_before": {"count": 0, "max_bottom": 0}},
    )

    verdict = _post_tap_verdict(
        step,
        _observation(
            screen=screen,
            nodes=[
                _text_node(3, sent_text, _bounds(screen, 0.48, 0.60, 0.94, 0.70)),
                _editable_node(1, "发消息或按住...", screen),
            ],
        ),
        f"打开与「七月」的聊天，并发送下面这段文本：{sent_text}",
    )

    assert verdict is not None
    assert "消息气泡" in verdict


def test_post_tap_verdict_does_not_treat_existing_same_text_bubble_as_new_send() -> None:
    screen = (1000, 2000)
    sent_text = "重复话术"
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": 2, "_locator_role": "send_button"},
        ok=True,
        message="tap_node(2 label='发送') -> clicked",
        elapsed_ms=87,
        obs_meta={"sent_echo_before": {"count": 1, "max_bottom": _bounds(screen, 0.48, 0.60, 0.94, 0.70)[3]}},
    )

    verdict = _post_tap_verdict(
        step,
        _observation(
            screen=screen,
            nodes=[
                _text_node(3, sent_text, _bounds(screen, 0.48, 0.60, 0.94, 0.70)),
                _editable_node(1, "发消息或按住...", screen),
            ],
        ),
        f"打开与「七月」的聊天，并发送下面这段文本：{sent_text}",
    )

    assert verdict is not None
    assert "输入框" in verdict
    assert "消息气泡" not in verdict


def test_media_post_tap_verdict_requires_gallery_send_button_role() -> None:
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": 20, "_locator_role": "chat_target"},
        ok=True,
        message="tap_node(20 label='Revornix交流群') -> clicked",
        elapsed_ms=87,
        obs_meta={"before_page": "HOME"},
    )

    verdict = _post_tap_verdict(
        step,
        _observation(package="com.tencent.wework", page="CHAT"),
        "文件名 dog.gif",
    )

    assert verdict is None


def test_media_post_tap_verdict_waits_when_gallery_send_still_unknown() -> None:
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": 10, "_locator_role": "gallery_send_button"},
        ok=True,
        message="tap_node(10 label='发送') -> clicked",
        elapsed_ms=87,
        obs_meta={"before_page": "UNKNOWN"},
    )

    verdict = _post_tap_verdict(
        step,
        _observation(package="com.tencent.wework", page="UNKNOWN"),
        "文件名 dog.gif",
    )

    assert verdict is not None
    assert "等待" in verdict
    assert "不要按 back" in verdict


def test_media_post_tap_verdict_accepts_gallery_send_returning_chat() -> None:
    step = AgentStep(
        index=1,
        thought="",
        action="tap_node",
        args={"node_id": 10, "_locator_role": "gallery_send_button"},
        ok=True,
        message="tap_node(10 label='发送') -> clicked",
        elapsed_ms=87,
        obs_meta={"before_page": "UNKNOWN"},
    )

    verdict = _post_tap_verdict(
        step,
        _observation(package="com.tencent.wework", page="CHAT"),
        "文件名 dog.gif",
    )

    assert verdict is not None
    assert "发送已生效" in verdict


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


def test_locator_cache_matches_same_device_even_when_node_id_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(react_locators, "_CACHE_DIR", tmp_path / "react_locator_cache")
    robot = _robot()
    screen = (1000, 2000)
    store = LocatorStore(robot)
    store.remember_success(
        role="send_button",
        action="tap_node",
        node=_button_node(2, "发送", screen),
        obs_meta={"screen_size": list(screen)},
        source="llm",
        screen_size=screen,
    )

    reloaded = LocatorStore(robot)
    matched = reloaded.match(
        "send_button",
        {99: _button_node(99, "发送", screen)},
        screen_size=screen,
    )

    assert matched is not None
    assert matched.id == 99


def test_locator_cache_rejects_different_device_fingerprint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(react_locators, "_CACHE_DIR", tmp_path / "react_locator_cache")
    screen = (1000, 2000)
    store = LocatorStore(_robot(model="Pixel 8", screen=screen))
    store.remember_success(
        role="send_button",
        action="tap_node",
        node=_button_node(2, "发送", screen),
        obs_meta={"screen_size": list(screen)},
        source="llm",
        screen_size=screen,
    )

    other_screen = (1200, 2400)
    reloaded = LocatorStore(_robot(model="Samsung S25", screen=other_screen))
    matched = reloaded.match(
        "send_button",
        {99: _button_node(99, "发送", other_screen)},
        screen_size=other_screen,
    )

    assert matched is None


def test_locator_cache_resets_old_entries_when_device_fingerprint_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(react_locators, "_CACHE_DIR", tmp_path / "react_locator_cache")
    screen = (1000, 2000)
    store = LocatorStore(_robot(model="Pixel 8", screen=screen))
    store.remember_success(
        role="send_button",
        action="tap_node",
        node=_button_node(2, "发送", screen),
        obs_meta={"screen_size": list(screen)},
        source="llm",
        screen_size=screen,
    )

    other_screen = (1200, 2400)
    changed = LocatorStore(_robot(model="Samsung S25", screen=other_screen))
    changed.remember_success(
        role="message_input",
        action="input_text",
        node=_editable_node(7, screen=other_screen),
        obs_meta={"screen_size": list(other_screen)},
        source="llm",
        screen_size=other_screen,
    )

    roles = [entry["role"] for entry in changed.data["locators"]]
    assert roles == ["message_input"]


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


def test_degraded_wecom_observation_detects_unknown_tiny_tree() -> None:
    obs = _observation(
        package="com.tencent.wework",
        page="UNKNOWN",
        nodes=[
            UiNode(id=i, cls="android.widget.FrameLayout", bounds=[0, 0, 100, 100])
            for i in range(1, 10)
        ],
    )

    assert _degraded_wecom_observation(obs) is True


def test_degraded_wecom_observation_allows_actionable_unknown_tree() -> None:
    obs = _observation(
        package="com.tencent.wework",
        page="UNKNOWN",
        nodes=[_editable_node(1, "")],
    )

    assert _degraded_wecom_observation(obs) is False


def test_degraded_wecom_observation_allows_media_preview_confirm_tree() -> None:
    obs = _observation(
        package="com.tencent.wework",
        page="UNKNOWN",
        nodes=[
            UiNode(id=1, cls="FrameLayout", bounds=[0, 0, 1440, 2200]),
            UiNode(id=2, cls="ViewGroup", view_id="ora", focusable=True, bounds=[0, 0, 1440, 1800]),
            UiNode(id=3, cls="ImageView", view_id="jm0", clickable=True, focusable=True, bounds=[360, 150, 1080, 870]),
            UiNode(id=4, cls="View", view_id="wq", clickable=True, focusable=True, bounds=[0, 0, 1440, 2200]),
            UiNode(id=5, cls="RelativeLayout", view_id="ne1", clickable=True, focusable=True, bounds=[0, 0, 1440, 120]),
            UiNode(id=6, cls="ImageView", view_id="h3d", clickable=True, focusable=True, bounds=[20, 20, 100, 100]),
            UiNode(id=7, cls="CheckBox", view_id="c5y", clickable=True, focusable=True, bounds=[1240, 20, 1320, 100]),
            UiNode(id=8, cls="RelativeLayout", view_id="b6c", clickable=True, focusable=True, bounds=[1120, 2040, 1420, 2180]),
            UiNode(id=9, cls="ViewGroup", view_id="lkd", clickable=True, focusable=True, bounds=[1160, 2070, 1400, 2160]),
            UiNode(id=10, cls="TextView", view_id="blz", text="发送", bounds=[1240, 2090, 1350, 2140]),
        ],
    )

    assert _degraded_wecom_observation(obs) is False


def test_node_expectation_captures_identity_fields_for_device_validation() -> None:
    node = UiNode(
        id=7,
        cls="android.widget.EditText",
        view_id="msg_input",
        text="发消息",
        desc="",
        editable=True,
        clickable=False,
        bounds=[1, 2, 3, 4],
    )

    assert _node_expectation(node) == {
        "cls": "android.widget.EditText",
        "view_id": "msg_input",
        "text": "发消息",
        "desc": "",
        "bounds": [1, 2, 3, 4],
        "editable": True,
        "clickable": False,
    }


def test_find_sent_message_echo_requires_matching_text_in_message_area() -> None:
    screen = (1000, 2000)
    sent_text = "hello"
    obs = _observation(
        screen=screen,
        nodes=[
            _text_node(1, sent_text, _bounds(screen, 0.48, 0.45, 0.95, 0.55)),
            _text_node(2, "other", _bounds(screen, 0.48, 0.60, 0.95, 0.70)),
        ],
    )

    assert _find_sent_message_echo(obs, sent_text).id == 1


def test_find_sent_message_echo_rejects_left_side_customer_text() -> None:
    screen = (1000, 2000)
    sent_text = "hello"
    obs = _observation(
        screen=screen,
        nodes=[_text_node(1, sent_text, _bounds(screen, 0.05, 0.45, 0.35, 0.55))],
    )

    assert _find_sent_message_echo(obs, sent_text) is None
