"""Live render smoke using voicelm's fake-page harness.

Every screen body executes through the REAL flet Renderer (not just
imports), and mounted components are unmounted afterwards so global
observable subscriptions cannot schedule updates against the fake page
from later tests.
"""

from __future__ import annotations

import contextlib

import flet as ft
import pytest
from flet.components.component import Component, Renderer
from flet.controls.context import _context_page

from core import tokens


class _FakeServices(list):
    def register_service(self, svc):
        with contextlib.suppress(Exception):
            list.append(self, svc)
        return svc

    def unregister_services(self):
        pass


class _FakeSession:
    def patch_control(self, prev_control, control, parent, path, frozen):
        pass

    def schedule_update(self, component):
        pass


class _FakePage:
    def __init__(self) -> None:
        self._services = _FakeServices()
        self.services: list = []
        self.platform = ft.PagePlatform.ANDROID
        self.theme_mode = ft.ThemeMode.DARK
        self.views = [ft.View()]
        self.dialog = None
        self.session = _FakeSession()

    def show_dialog(self, d) -> None:
        self.dialog = d

    def pop_dialog(self) -> None:
        self.dialog = None

    def update(self) -> None:
        pass

    def run_task(self, *args, **kwargs) -> None:
        pass

    def run_thread(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def _renderer_page():
    page = _FakePage()
    _context_page.set(page)
    return page


def _render(component_fn):
    """Render a component AND execute its body, recursively.

    `Renderer().render()` only wraps the callable; the body first runs during
    `before_update()`. Calling render() alone let screens that raise on their
    first constructed control (e.g. Container(scroll=...)) report as passing.

    `before_update()` on a Component only renders ITS own body. Nested
    components returned by it (Composer, SessionBar, ThinkingBlock...) build
    later, during the real page patch — which is how `Chip(avatar=...)` (not
    a flet 1.0 property) reached production unnoticed. `_deep_update` walks
    the whole tree so nested components are executed here too.
    """
    component = Renderer().render(component_fn)
    _deep_update(component)
    return component


def _deep_update(control, depth: int = 0) -> None:
    """Execute before_update on `control` and every nested Component."""
    if control is None or depth > 30:
        return
    if isinstance(control, Component):
        control.before_update()
        for child in _flatten(getattr(control, "_b", None)):
            _deep_update(child, depth + 1)
        return
    # flet_ads controls resolve self.page by walking a real _parent chain to a
    # live Page; the synthetic test tree has none, so they raise here even
    # though they are fine in the app. Skip them rather than fake a parent.
    if type(control).__module__.startswith("flet_ads"):
        return
    # Plain controls: run their own validators, then recurse.
    control.before_update()
    for child in _children(control):
        _deep_update(child, depth + 1)


def _flatten(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _children(control):
    """Best-effort child extraction across flet's control shapes."""
    out = []
    for attr in ("controls", "content", "leading", "trailing", "items", "views"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            out.extend(v for v in value if v is not None)
        elif value is not None and hasattr(value, "__dict__"):
            out.append(value)
    return out


def _walk_all(node, depth: int = 0):
    """Yield every control in a tree, crossing Component bodies.

    Unlike `_children` this also crosses into nested components (`_b`) and
    dialog parts (`title`/`actions`), so tests can assert on controls that
    live several layers deep without hand-rolling a walker each time.
    """
    if node is None or depth > 80:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk_all(item, depth + 1)
        return
    if isinstance(node, Component):
        yield from _walk_all(getattr(node, "_b", None), depth + 1)
        return
    yield node
    for attr in ("controls", "content", "title", "actions", "leading", "trailing", "items"):
        yield from _walk_all(getattr(node, attr, None), depth + 1)


def test_all_screens_render(_renderer_page):
    from app_shell import AppShell
    from core.state import state
    from screens.chat_screen import ChatScreen
    from screens.history_screen import HistoryScreen
    from screens.onboarding_screen import OnboardingScreen
    from screens.server_screen import ServerScreen
    from screens.settings_screen import SettingsScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    # Exercise the populated branches too, not only the empty states.
    state.onboarding_done = True
    state.gateway_running = True
    state.gateway_lan_url = "http://192.168.1.5:8082/v1"
    state.messages = [{"role": "user", "content": "hello"}]
    state.conversations = [
        {"id": "abc", "title": "Test chat", "relative": "now", "updated": "2026-01-01"}
    ]

    roots = []
    try:
        for component in (
            OnboardingScreen,
            ChatScreen,
            ServerScreen,
            SettingsScreen,
            HistoryScreen,
        ):
            roots.append(_render(component))
        shell_root = _render(
            lambda: ControllerMethodsCtx(ControllerMethods(), lambda: AppShell()),
        )
        roots.append(shell_root)
        assert shell_root is not None
    finally:
        for root in roots:
            with contextlib.suppress(Exception):
                root._detach_observable_subscriptions()
                root._state.mounted = False


def test_chat_banners_scroll_with_messages_and_skip_empty_conversations(_renderer_page):
    from core.state import state
    from screens.chat_screen import BANNER_AD_EVERY_N_REPLIES, ChatScreen

    previous_messages = state.messages
    state.gateway_running = True
    state.messages = [
        {"role": "assistant", "content": "first reply"},
        {"role": "assistant", "content": "second reply"},
        {"role": "assistant", "content": "third reply"},
    ]

    populated = _render(ChatScreen)
    try:
        assert populated is not None
        root = populated._b
        assert isinstance(root, ft.Column)
        # Chat used to append a fourth root-level banner; the only ListView
        # must contain the message rows and remain the auto-scrolling viewport.
        assert len(root.controls) == 3
        message_list = root.controls[1]
        assert isinstance(message_list, ft.ListView)
        assert message_list.auto_scroll is True

        message_rows = message_list.controls
        # A banner must never be the last row: the owner bans floor banners
        # ("remove that banner at the floor"), so the reply after the final
        # message gets no banner.
        last_reply_index = len(state.messages) - 1
        expected_banner_positions = [
            2 * reply_index + 1
            for reply_index in range(len(state.messages))
            if (reply_index + 1) % BANNER_AD_EVERY_N_REPLIES == 0 and reply_index < last_reply_index
        ]
        actual_banner_positions = [
            index for index, row in enumerate(message_rows) if isinstance(row, ft.Row)
        ]
        assert actual_banner_positions == expected_banner_positions
        assert all(isinstance(message_rows[index], ft.Row) for index in expected_banner_positions)
    finally:
        populated._detach_observable_subscriptions()
        populated._state.mounted = False

    state.messages = []
    empty = _render(ChatScreen)
    try:
        assert empty is not None
        root = empty._b
        assert isinstance(root, ft.Column)
        assert len(root.controls) == 3
        message_list = root.controls[1]
        assert isinstance(message_list, ft.ListView)
        assert not any(isinstance(row, ft.Row) for row in message_list.controls)
    finally:
        empty._detach_observable_subscriptions()
        empty._state.mounted = False
        state.messages = previous_messages


def test_chat_action_row_shows_visible_actions(_renderer_page):
    """Every bubble needs a visible Copy/Edit/Regenerate row.

    The gesture-only menu was the SOLE affordance for these actions and it
    never opened (flet 1.0 reads per-trigger item lists); DDGS ships a plain
    icon row because long press alone is not discoverable and has no keyboard
    equivalent. This pins the row's presence per role and its gating.
    """
    from core.state import state
    from screens.chat_screen import ChatScreen

    previous_messages = state.messages
    state.gateway_running = True
    state.busy = False
    state.messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

    root = _render(ChatScreen)
    try:
        tooltips = {
            node.tooltip
            for node in _walk_all(root)
            if isinstance(node, ft.IconButton) and node.tooltip
        }
        assert {"Copy", "Edit & resend", "Regenerate"} <= tooltips, (
            f"visible action row incomplete, found tooltips: {sorted(tooltips)}"
        )
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.messages = previous_messages


def test_chat_action_row_hides_last_turn_actions_while_busy(_renderer_page):
    """Edit/Regenerate must vanish mid-stream; Copy on a finished reply stays."""
    from core.state import state
    from screens.chat_screen import ChatScreen

    previous_messages = state.messages
    state.gateway_running = True
    state.busy = True
    state.messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": ""},
    ]

    root = _render(ChatScreen)
    try:
        tooltips = {
            node.tooltip
            for node in _walk_all(root)
            if isinstance(node, ft.IconButton) and node.tooltip
        }
        assert "Regenerate" not in tooltips
        assert "Edit & resend" not in tooltips
        # User-side Copy persists mid-stream (copying the question is always
        # valid); assistant Copy only exists once content has arrived.
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.busy = False
        state.messages = previous_messages


def test_chat_error_rows_offer_recovery_actions(_renderer_page, monkeypatch) -> None:
    """Every typed error names its own way forward (DDGS parity): rate
    limits get a one-tap switch-and-retry, offline gets Start gateway, and
    anything else gets Try again — a dead red line is not an affordance."""
    from core.state import state
    from screens.chat_screen import ChatScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    monkeypatch.setattr(
        "screens.chat_screen.rate_limit_suggestion",
        lambda model, models: "alt-model",
    )
    previous = state.messages
    state.gateway_running = True
    state.busy = False
    state.messages = [
        {"role": "user", "content": "q"},
        {"role": "error", "content": "Rate limited by this model.", "kind": "rate_limited"},
        {"role": "error", "content": "Gateway unreachable.", "kind": "offline"},
        {"role": "error", "content": "Model setup failed.", "kind": "setup"},
        {"role": "error", "content": "legacy row without a kind"},
    ]

    calls: list[str] = []
    methods = ControllerMethods()
    methods.regenerate_last = lambda: calls.append("regen")
    methods.start_gateway = lambda: calls.append("start")
    methods.set_model = lambda model: calls.append(f"model:{model}")

    root = _render(lambda: ControllerMethodsCtx(methods, ChatScreen))
    try:
        buttons = {
            getattr(node, "content", None)
            for node in _walk_all(root)
            if isinstance(node, ft.TextButton)
        }
        assert "Use alt-model and retry" in buttons, buttons
        assert "Start gateway" in buttons, buttons
        assert "Try again" in buttons, buttons
        try_count = sum(
            1
            for node in _walk_all(root)
            if isinstance(node, ft.TextButton) and getattr(node, "content", None) == "Try again"
        )
        assert try_count == 1, f"only the setup kind gets Try again here; got {try_count}"

        def _click(label: str) -> None:
            for node in _walk_all(root):
                if isinstance(node, ft.TextButton) and getattr(node, "content", None) == label:
                    node.on_click(None)
                    return
            raise AssertionError(f"button {label!r} not found")

        _click("Try again")
        _click("Use alt-model and retry")
        _click("Start gateway")
        assert calls == ["regen", "model:alt-model", "regen", "start"], calls
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.messages = previous


def test_server_screen_status_words_are_honest(_renderer_page) -> None:
    """`untested` is the wire word for a capped model; the count bucket is
    untested+slow — neither may be shown as plain 'rate limited' count or
    raw 'untested'."""
    from core.state import state
    from screens.server_screen import ServerScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    saved = (
        state.gateway_running,
        state.gateway_counts,
        state.models,
        state.share_url,
        state.gateway_starting,
    )
    state.gateway_running = True
    state.gateway_counts = {"models": {"active": 3, "degraded": 2, "failed": 1}}
    state.models = [
        {"id": "capped-model", "status": "untested"},
        {"id": "ok-model", "status": "active"},
    ]
    state.share_url = ""
    try:
        root = _render(lambda: ControllerMethodsCtx(ControllerMethods(), ServerScreen))
        texts: list[str] = []
        for node in _walk_all(root):
            if isinstance(node, str):
                texts.append(node)
            else:
                value = getattr(node, "value", None)
                if isinstance(value, str):
                    texts.append(value)
        blob = " ".join(texts)
        assert "2 capped or slow" in blob, blob[:500]
        assert "2 rate limited" not in blob, "the mixed bucket must not claim rate limiting"
        assert "rate limited" in blob, "the untested row must read 'rate limited'"
        assert "untested" not in blob, "the raw wire word must never reach the user"
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        (
            state.gateway_running,
            state.gateway_counts,
            state.models,
            state.share_url,
            state.gateway_starting,
        ) = saved


def test_server_header_exposes_the_activity_log_and_notice_keeps_remedy(
    _renderer_page,
) -> None:
    """The log-terminal dialog existed with ZERO callers while three notices
    told users to 'See logs.'; the shell banner must also keep enough lines
    to show the remedy, not just the diagnosis."""
    from app_shell import AppShell
    from core.state import state
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    saved = (state.notice, state.onboarding_done, state.selected_tab)
    state.notice = "x" * 160
    state.onboarding_done = True
    state.selected_tab = 1  # AppShell mounts only the selected view: Server
    calls: list[str] = []
    methods = ControllerMethods()
    methods.open_log_terminal = lambda: calls.append("log")

    root = _render(lambda: ControllerMethodsCtx(methods, lambda: AppShell()))
    try:
        log_buttons = [
            node
            for node in _walk_all(root)
            if isinstance(node, ft.IconButton) and getattr(node, "tooltip", None) == "Activity log"
        ]
        assert log_buttons, "the Server header must expose the Activity log"
        log_buttons[0].on_click(None)
        assert calls == ["log"]

        notice_texts = [
            node
            for node in _walk_all(root)
            if isinstance(node, ft.Text) and getattr(node, "value", None) == state.notice
        ]
        assert notice_texts, "the notice banner must render"
        assert notice_texts[0].max_lines == 5, "the remedy line was being ellipsised away"
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.notice, state.onboarding_done, state.selected_tab = saved


def test_navigation_bar_attaches_to_the_shell_view(_renderer_page):
    """The back underlay makes page.views[0] the never-visible blank view —
    attaching the NavigationBar there hid the bottom navigation entirely
    (owner device regression). It must land on the TOP (shell) view.

    Called directly because flet only flushes use_effect callbacks against a
    live session, which the render harness has none of; the wiring pin below
    keeps the effect hooked to this function.
    """
    from app_shell import AppShell, _sync_navigation_bar
    from core.state import state
    from state.controller_ctx import ControllerMethods

    page = _renderer_page
    shell = page.views[0]
    shell.route = "/"
    # Exactly what AppController._ensure_back_underlay inserts after render.
    page.views.insert(0, ft.View(route="/blank", bgcolor=ft.Colors.BLACK, padding=0))
    blank = page.views[0]

    saved = (state.onboarding_done, state.selected_tab)
    state.onboarding_done = True
    state.selected_tab = 0
    try:
        _sync_navigation_bar(page, state, ControllerMethods())
        assert shell.navigation_bar is not None, (
            "the NavigationBar must attach to the shell (top view)"
        )
        assert blank.navigation_bar is None, (
            "the NavigationBar must never land on the back underlay"
        )
        assert shell.navigation_bar.selected_index == 0

        # onboarding gate: bar removed again when onboarding owns the screen
        state.onboarding_done = False
        _sync_navigation_bar(page, state, ControllerMethods())
        assert shell.navigation_bar is None
    finally:
        state.onboarding_done, state.selected_tab = saved
        page.views[:] = [shell]

    # Wiring pin: the component's effect must still call the hoisted sync
    # (the effect itself never flushes in this harness).
    import inspect

    source = inspect.getsource(AppShell)
    assert "_sync_navigation_bar(page, state, methods)" in source


def test_server_log_filter_uses_chat_scale_pills(_renderer_page) -> None:
    """The old Material Chips out-sized the log lines they filter (owner
    complaint). The bar must be compact pills at the chat row's scale."""
    from core.state import state
    from screens.server_screen import ServerScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    saved = (state.gateway_running, state.onboarding_done)
    state.gateway_running = True
    state.onboarding_done = True
    root = _render(lambda: ControllerMethodsCtx(ControllerMethods(), ServerScreen))
    try:
        nodes = list(_walk_all(root))
        assert not any(isinstance(node, ft.Chip) for node in nodes), (
            "Material Chips are back and they out-size the log rows"
        )
        labels = {
            getattr(node, "value", None): getattr(node, "size", None)
            for node in nodes
            if isinstance(node, ft.Text)
            and getattr(node, "value", None) in ("ALL", "INFO", "WARNING", "ERROR")
        }
        assert set(labels) == {"ALL", "INFO", "WARNING", "ERROR"}, labels
        assert all(size == tokens.FONT_XS for size in labels.values()), labels
        # The pills are tappable containers, not dead text.
        pill_clicks = [
            node
            for node in nodes
            if isinstance(node, ft.Container)
            and node.on_click is not None
            and isinstance(node.content, ft.Text)
            and getattr(node.content, "value", None) in ("ALL", "INFO", "WARNING", "ERROR")
        ]
        assert len(pill_clicks) == 4, "each filter pill must be tappable"
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.gateway_running, state.onboarding_done = saved


def test_catalog_rows_carry_the_bench_pill(_renderer_page) -> None:
    """Console parity: every catalog row shows a Test pill — idle, in-flight
    (spinner), or a colored verdict that taps through to a re-test."""
    from core.state import state
    from screens.server_screen import ServerScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    saved = (
        state.gateway_running,
        state.models,
        state.model_testing,
        state.model_test_results,
        state.retesting,
        state.retest_progress,
    )
    state.gateway_running = True
    state.models = [
        {"id": "m-idle", "status": "untested"},
        {"id": "m-busy", "status": "active"},
        {"id": "m-done", "status": "active"},
        {"id": "m-rate", "status": "untested"},
    ]
    state.model_testing = frozenset({"m-busy"})
    state.model_test_results = {
        "m-done": {"id": "m-done", "verdict": "OK", "ms": 842, "snippet": "OK"},
        "m-rate": {"id": "m-rate", "verdict": "RATE", "ms": 4000, "snippet": ""},
    }
    state.retesting = False
    state.retest_progress = (0, 0)
    try:
        root = _render(lambda: ControllerMethodsCtx(ControllerMethods(), ServerScreen))
        texts = {
            getattr(node, "value", None)
            for node in _walk_all(root)
            if isinstance(node, ft.Text) and getattr(node, "value", None)
        }
        assert "Test" in texts, "idle rows must offer a Test pill"
        assert "testing" in texts, "in-flight rows must show the spinner label"
        assert "✓ 842ms" in texts, texts
        assert "✗ rate limited" in texts, "429 verdict uses the owner's plain wording"
        buttons = {
            getattr(node, "content", None)
            for node in _walk_all(root)
            if isinstance(node, ft.TextButton)
        }
        assert "Retest not-ready (2)" in buttons, f"header count wrong: {buttons}"
        assert "Test all" in buttons
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        (
            state.gateway_running,
            state.models,
            state.model_testing,
            state.model_test_results,
            state.retesting,
            state.retest_progress,
        ) = saved


def test_catalog_header_drives_the_bench(_renderer_page) -> None:
    """The header buttons must reach the controller: retest-not-ready passes
    True, Test all passes False, and Stop appears only while a sweep runs."""
    from core.state import state
    from screens.server_screen import ServerScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    saved = (
        state.gateway_running,
        state.models,
        state.retesting,
        state.retest_progress,
    )
    state.gateway_running = True
    state.models = [{"id": "a", "status": "active"}, {"id": "b", "status": "slow"}]
    state.retesting = False
    state.retest_progress = (0, 0)

    calls: list = []
    methods = ControllerMethods()
    methods.retest_models = lambda only: calls.append(("retest", only))
    methods.stop_retest = lambda: calls.append(("stop",))
    try:
        root = _render(lambda: ControllerMethodsCtx(methods, ServerScreen))

        def _click(label: str) -> None:
            for node in _walk_all(root):
                if isinstance(node, ft.TextButton) and getattr(node, "content", None) == label:
                    node.on_click(None)
                    return
            raise AssertionError(f"button {label!r} not found")

        _click("Retest not-ready (1)")
        _click("Test all")
        assert calls == [("retest", True), ("retest", False)], calls
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.gateway_running, state.models, state.retesting, state.retest_progress = saved

    # While a sweep runs: progress label + Stop, no retest buttons.
    state.gateway_running = True
    state.models = [{"id": "a", "status": "active"}]
    state.retesting = True
    state.retest_progress = (3, 7)
    try:
        root = _render(lambda: ControllerMethodsCtx(methods, ServerScreen))
        texts = {
            getattr(node, "value", None)
            for node in _walk_all(root)
            if isinstance(node, ft.Text) and getattr(node, "value", None)
        }
        assert "Testing 3/7…" in texts, texts
        for node in _walk_all(root):
            if isinstance(node, ft.TextButton) and getattr(node, "content", None) == "Stop":
                node.on_click(None)
                break
        else:
            raise AssertionError("Stop button missing during a sweep")
        assert ("stop",) in calls
    finally:
        root._detach_observable_subscriptions()
        root._state.mounted = False
        state.gateway_running, state.models, state.retesting, state.retest_progress = saved
