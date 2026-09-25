"""House-parity regression gates.

Each test pins one owner complaint from the 2026-09-25 UI overhaul:
SafeArea/status bar, KTV View-chrome nav, Sherlock header values, the
scrollable session strip, the removed attachment feature, single log
header, banner placement (never at the floor), share error visibility,
compact server buttons, and full-width settings cards.

Source-level assertions are deliberate: they guard structure that a
rendered tree cannot express (where a nav bar lives, which logs are
warnings).
"""

from __future__ import annotations

from pathlib import Path

import flet as ft
import pytest
from test_render import _FakePage, _render

from core.state import state

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def _squish(text: str) -> str:
    """Collapse whitespace so multi-line calls still match as substrings."""
    return " ".join(text.split())


def _code_only(src: str) -> str:
    """Strip triple-quoted strings so docstrings cannot satisfy an assert."""
    out: list[str] = []
    i = 0
    while i < len(src):
        if src.startswith('"""', i) or src.startswith("'''", i):
            quote = src[i : i + 3]
            end = src.find(quote, i + 3)
            i = (end + 3) if end != -1 else len(src)
            out.append('""""""')
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


@pytest.fixture
def _renderer_page():
    """Same fake page harness as test_render (fixture-safe local copy)."""
    from flet.controls.context import _context_page

    page = _FakePage()
    _context_page.set(page)
    return page


def _walk(node, depth: int = 0):
    """Yield every control in a rendered tree (Component._b included)."""
    if node is None or depth > 80:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item, depth + 1)
        return
    from flet.components.component import Component

    if isinstance(node, Component):
        yield from _walk(getattr(node, "_b", None), depth + 1)
        return
    yield node
    for attr in ("controls", "content"):
        yield from _walk(getattr(node, attr, None), depth + 1)


def _banner_sentinel() -> ft.Container:
    return ft.Container(key=ft.ValueKey("parity-banner"))


def _banner_indices(controls: list) -> list[int]:
    return [
        i for i, c in enumerate(controls) if getattr(c, "key", None) == ft.ValueKey("parity-banner")
    ]


# ---------------------------------------------------------------- SafeArea


def test_shell_root_is_safe_area_in_both_branches(_renderer_page) -> None:
    from app_shell import AppShell
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    previous = state.onboarding_done
    try:
        for onboarding in (False, True):
            state.onboarding_done = onboarding
            root = _render(
                lambda: ControllerMethodsCtx(ControllerMethods(), lambda: AppShell()),
            )
            assert root is not None
            built = root._b
            assert isinstance(built, ft.SafeArea), (
                f"onboarding_done={onboarding}: shell root is {type(built).__name__}, "
                "not ft.SafeArea (header will sit under the Android status bar)"
            )
    finally:
        state.onboarding_done = previous


def test_page_chrome_zeroed_and_nav_is_view_chrome() -> None:
    shell = _read("app_shell.py")
    main = _read("main.py")
    theme = _read("core/theme.py")

    assert "page.padding = 0" in main and "page.spacing = 0" in main
    # KTV pattern: nav lives on the View, never inside the shell Column,
    # and its height is never overridden.
    assert "page.views[0]" in shell and "view.navigation_bar" in shell
    assert "_sync_navigation_bar" in shell
    assert "ft.use_effect" in shell
    assert "height=64" not in shell, "hardcoded nav height is back"
    assert shell.count("ft.NavigationBar(") == 1, "nav must be built in exactly one place"
    assert theme.count("navigation_bar_theme") == 2, "both themes need NavigationBarTheme"


def test_header_carries_sherlock_values() -> None:
    header = _squish(_read("components/app_header.py"))
    # Sherlock AppHeader: 16/8 padding, horizontal overflow scroll,
    # left gap 4, right gap 2.
    assert "tokens.SPACE_LG, tokens.SPACE_SM, tokens.SPACE_LG, tokens.SPACE_SM" in header
    assert "scroll=ft.ScrollMode.AUTO" in header
    assert "spacing=tokens.SPACE_XS" in header  # left row
    assert "spacing=tokens.SPACE_XXS" in header  # right row


# ------------------------------------------------------------- chat strip


def test_session_strip_scrollable_and_width_constrained(_renderer_page) -> None:
    from screens.chat_screen import ChatScreen

    root = _render(ChatScreen)
    assert root is not None
    controls = list(_walk(root._b))
    assert controls, "chat screen rendered nothing"

    inf = [c for c in controls if getattr(c, "width", None) == float("inf")]
    assert not inf, "SessionBar still claims width=float('inf') (pushes MCP off-screen)"

    scrollable_rows = [
        c
        for c in controls
        if isinstance(c, ft.Row)
        and getattr(c, "scroll", None) is not None
        and getattr(c, "wrap", None) is False
    ]
    assert scrollable_rows, (
        "model/Internet/MCP strip has no horizontal scroll (Sherlock chip pattern)"
    )


def test_attachment_feature_removed() -> None:
    for rel in ("components/chat_controls.py", "screens/chat_screen.py"):
        src = _read(rel)
        for token in (
            "FilePicker",
            "pick_files",
            "ATTACH_FILE",
            "Attached file",
            "allowed_extensions",
        ):
            assert token not in src, f"{rel} still references {token!r}"
    # nowhere in the app may the chat attach picker remain
    for path in (SRC / "screens").glob("*.py"):
        assert "pick_files" not in path.read_text(encoding="utf-8"), path.name


# ------------------------------------------------------------ server/logs


def test_server_single_log_header_scrollable_and_banner_order(_renderer_page, monkeypatch) -> None:
    import screens.server_screen as server

    monkeypatch.setattr(server, "build_banner_ad", _banner_sentinel)
    from screens.server_screen import ServerScreen

    previous = state.gateway_running
    state.gateway_running = True
    try:
        root = _render(ServerScreen)
        assert root is not None
        page_column = root._b
        assert isinstance(page_column, ft.Column)
        controls = page_column.controls

        texts = [
            c.value
            for c in _walk(page_column)
            if isinstance(c, ft.Text) and isinstance(c.value, str)
        ]
        logs_headers = [t for t in texts if t == "Logs"]
        assert len(logs_headers) == 1, f"'Logs' header rendered {len(logs_headers)} times"
        assert sum(1 for t in texts if t.endswith(" lines")) == 1

        banners = _banner_indices(controls)
        assert len(banners) == 2, f"expected 2 banners, found {len(banners)} at {banners}"

        def _idx(predicate) -> int:
            for i, c in enumerate(controls):
                subtree = [n.value for n in _walk(c) if isinstance(n, ft.Text)]
                if predicate(subtree):
                    return i
            return -1

        catalog_i = _idx(lambda vals: any(v and v.startswith("Model Catalog") for v in vals))
        logs_i = _idx(lambda vals: "Logs" in vals)
        assert catalog_i != -1 and logs_i != -1, "catalog or log card missing from the page"
        assert banners[0] < catalog_i < banners[1] < logs_i, (
            f"banner order wrong: banners={banners}, catalog={catalog_i}, logs={logs_i}"
        )
        assert logs_i == len(controls) - 1, (
            "the log card must be the last control (no floor banner)"
        )

        log_lists = [
            c
            for c in _walk(page_column)
            if isinstance(c, ft.ListView)
            and getattr(c, "auto_scroll", None) is not True
            and len(getattr(c, "controls", []) or []) > 0
            and any(isinstance(r, ft.Text) for r in _walk(c.controls))
        ]
        assert log_lists, "log ListView missing or still auto-scrolling (yanks away from errors)"
        assert not any(
            isinstance(c, ft.ListView) and getattr(c, "auto_scroll", None)
            for c in _walk(page_column)
        ), "an auto_scroll ListView remains (user can never scroll the logs)"
    finally:
        state.gateway_running = previous


def test_share_state_is_observable_and_failures_loud() -> None:
    from core.state import AppState

    assert hasattr(AppState, "share_starting"), "double-click guard field missing"
    assert hasattr(AppState, "share_error"), "inline share error field missing"

    main = _read("main.py")
    assert "self._share_error" not in main, "dead non-observable _share_error is back"
    assert "share state update failed" in main, "done() is not guarded against silent failure"
    assert "share_starting" in main

    tunnel = _read("services/tunnel.py")
    claim_line = next(line for line in tunnel.splitlines() if "claim failed, retrying" in line)
    assert "LOG.warning" in claim_line, "tunnel claim retries are invisible again (was LOG.debug)"

    server = _read("screens/server_screen.py")
    assert "state.share_error" in server, "share card no longer renders the error inline"


def test_server_buttons_are_compact(_renderer_page) -> None:
    from screens.server_screen import ServerScreen

    root = _render(ServerScreen)
    assert root is not None
    buttons = [c for c in _walk(root._b) if isinstance(c, (ft.FilledButton, ft.OutlinedButton))]
    styled = [b for b in buttons if getattr(b, "style", None) is not None]
    assert len(styled) >= 3, (
        f"only {len(styled)}/{len(buttons)} buttons carry the compact style; "
        "Stop/Refresh/Open console must be compact and on one line"
    )


# ------------------------------------------------------ settings / history


def test_settings_cards_full_width_and_no_floor_banner(_renderer_page, monkeypatch) -> None:
    import screens.settings_screen as settings_mod

    kit = _read("components/settings_kit.py")
    # Only settings_card's wrapper Column must not be tight; setting_row's
    # title column legitimately uses tight=True (Sherlock does the same).
    card_src = kit.split("def settings_card", 1)[1].split("\ndef ", 1)[0]
    assert "tight=True" not in card_src, (
        "settings_card Column is tight again (cards hug content, look tiny)"
    )

    monkeypatch.setattr(settings_mod, "build_banner_ad", _banner_sentinel)
    from screens.settings_screen import SettingsScreen

    root = _render(SettingsScreen)
    assert root is not None
    list_view = root._b
    assert isinstance(list_view, ft.ListView)
    controls = list_view.controls

    banners = _banner_indices(controls)
    assert len(banners) == 2, f"settings should keep 2 mid-page banners, found {banners}"
    assert (len(controls) - 1) not in banners, "floor banner is back on Settings"
    last = controls[-1]
    assert isinstance(last, ft.Container) and last.height is not None, (
        "Settings must end with the bottom spacer, not an ad"
    )


def test_history_has_no_banner(_renderer_page) -> None:
    from screens.history_screen import HistoryScreen

    # The import itself was removed: there is nothing to render or patch.
    assert "build_banner_ad" not in _read("screens/history_screen.py"), (
        "History re-imported the banner (floor banners banned)"
    )

    root = _render(HistoryScreen)
    assert root is not None
    found = [c for c in _walk(root._b) if getattr(c, "key", None) == ft.ValueKey("parity-banner")]
    assert not found, "History must not render any banner (floor banners banned)"


# ------------------------------------------------------- round-2 complaints


def _iter_string_constants(path: Path):
    """Yield non-docstring string literals from a Python file."""
    import ast

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_ids.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_ids:
                yield node.value


def test_ui_strings_have_no_emdash_or_friend() -> None:
    """The owner bans em-dashes and the word 'friend' in anything the user reads."""
    # chr() keeps the banned characters out of this source file so RUF001
    # does not flag the test that enforces the ban.
    banned_dashes = (chr(0x2014), chr(0x2013))
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for value in _iter_string_constants(path):
            if any(dash in value for dash in banned_dashes):
                offenders.append(f"{path.relative_to(SRC)}: em-dash in {value!r}")
            if "friend" in value.lower():
                offenders.append(f"{path.relative_to(SRC)}: 'friend' in {value!r}")
    assert not offenders, "banned copy found:\n" + "\n".join(offenders)


def test_server_screen_renders_on_settings_save() -> None:
    """The Require-API-key switch saves via settings_version; the screen must subscribe."""
    server = _read("screens/server_screen.py")
    assert "state.settings_version" in server, (
        "ServerScreen does not read state.settings_version: toggling "
        "'Require API key' re-renders nothing and the key field never appears"
    )
    settings = _read("screens/settings_screen.py")
    assert "if _notice" in settings, (
        "Settings validation/success notices are set but never rendered"
    )
    assert settings.count("set_notice(") >= 10


def test_catalog_is_bounded_and_logs_never_autoscroll() -> None:
    server = _read("screens/server_screen.py")
    assert "tokens.CATALOG_VIEWPORT" in server, (
        "Model catalog has no bounded vertical viewport (owner: cannot scroll it)"
    )
    assert "auto_scroll=True" not in server, (
        "server screen must never auto-scroll: it yanks the view away "
        "from the log line or model the user is reading"
    )
    # Every multi-child row that can overflow must be horizontally scrollable:
    # the owner's repeated 'scrolling sideways' instruction.
    assert server.count("scroll=ft.ScrollMode.AUTO") >= 5, (
        f"expected sideway-scrollable rows, found {server.count('scroll=ft.ScrollMode.AUTO')}"
    )


def test_console_logging_exists() -> None:
    """`uv run flet run` must print logs; ring+file alone showed nothing."""
    logging_src = _read("core/logging.py")
    assert "StreamHandler" in logging_src, "no console handler: dev runs log nothing"
