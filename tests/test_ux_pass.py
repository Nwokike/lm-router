"""UX pass regression: GuardedKani already covered; this pins B/C/D."""

from __future__ import annotations

from test_boot_smoke import boot_page
from test_render import _render, _renderer_page, _walk_all

__all__ = ["_renderer_page", "boot_page"]


def test_builtin_exa_seeded_once() -> None:
    from core.settings import AppSettings

    first = AppSettings.from_stored({})
    builtin = [s for s in first.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin} == {"builtin-exa", "builtin-parallel"}
    assert all(s.enabled for s in builtin)

    # Second load from the stored form: no duplicates.
    stored = first.to_stored()
    second = AppSettings.from_stored(stored)
    builtin2 = [s for s in second.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin2} == {"builtin-exa", "builtin-parallel"}


def test_manual_exa_counts_as_present() -> None:
    from core.settings import AppSettings

    stored = {
        "mcp_servers": [
            {
                "id": "user-exa",
                "name": "Exa",
                "transport": "streamable_http",
                "url": "https://mcp.exa.ai/mcp",
                "enabled": True,
            }
        ]
    }
    settings = AppSettings.from_stored(stored)
    builtin = [s for s in settings.mcp_servers if getattr(s, "protected", False)]
    # Manual Exa suppresses the builtin Exa twin, but Parallel still seeds.
    assert [s.id for s in builtin] == ["builtin-parallel"]
    assert len(settings.mcp_servers) == 2


def test_protected_remove_refused(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    builtin = [s for s in controller.settings.mcp_servers if getattr(s, "protected", False)]
    assert {s.id for s in builtin} == {"builtin-exa", "builtin-parallel"}
    before = len(controller.settings.mcp_servers)
    for s in builtin:
        controller._remove_mcp_server(s.id)
    assert len(controller.settings.mcp_servers) == before


def test_no_search_pill_in_session_bar(_renderer_page) -> None:
    from components.chat_controls import SessionBar
    from core.state import state
    from state.controller_ctx import ControllerMethods

    tree = _render(lambda: SessionBar(state=state, methods=ControllerMethods(), is_dark=False))
    texts = [str(getattr(n, "value", "")) for n in _walk_all(tree) if type(n).__name__ == "Text"]
    blob = " ".join(texts)
    assert "Internet" not in blob, f"search pill still rendered: {blob[:400]}"
    assert "Tools" in blob


def test_chat_header_has_new_chat(_renderer_page) -> None:
    from app_shell import AppShell
    from core.state import state
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    state.onboarding_done = True
    state.selected_tab = 0
    try:
        tree = _render(lambda: ControllerMethodsCtx(ControllerMethods(), lambda: AppShell()))
    finally:
        state.onboarding_done = False
        state.selected_tab = 0
    tips = set()
    for n in _walk_all(tree):
        tip = getattr(n, "tooltip", None)
        if isinstance(tip, str) and tip:
            tips.add(tip)
    assert "New chat" in tips, f"plus button missing; tips={sorted(tips)[:20]}"
    assert "History" in tips
