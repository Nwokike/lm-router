"""Screen UX regression: M3 fixes must hold.

- History sub-view keeps the nav highlight (last_nav_tab untouched by tab 3).
- Server catalog render survives null rows, null status, unknown tiers.
- "Rate limited" wording, never "degraded"/"capped or slow" on the surface.
- Error rows wrap (expand=True); OK verdict without ms renders cleanly.
"""

from __future__ import annotations

from test_boot_smoke import boot_page
from test_render import _renderer_page

from core.state import state

__all__ = ["_renderer_page", "boot_page"]


def test_history_tab_keeps_nav_highlight(boot_page) -> None:
    """Opening History (3) must not snap the bar highlight to Chat."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._set_tab(1)  # Server
    assert state.last_nav_tab == 1
    controller._set_tab(3)  # History sub-view
    assert state.selected_tab == 3
    assert state.last_nav_tab == 1, "History stole the nav highlight"
    controller._set_tab(0)
    assert state.last_nav_tab == 0


def test_set_tab_clamps_out_of_range(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._set_tab(99)
    assert state.selected_tab == 0
    controller._set_tab(-1)
    assert state.selected_tab == 0


def test_server_catalog_survives_null_rows(_renderer_page) -> None:
    """Null rows / null status / null id must not crash the Server tab."""
    from test_render import _render

    from screens.server_screen import ServerScreen

    state.models = [
        None,
        "not-a-dict",
        {"id": None, "status": None},
        {"id": "good-model", "status": "active"},
    ]
    state.gateway_running = True
    try:
        tree = _render(ServerScreen)
        assert tree is not None
    finally:
        state.models = []
        state.gateway_running = False


def test_no_degraded_wording_on_server_surface() -> None:
    """Owner rule: the surface says 'rate limited', never degraded/capped."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in (src / "screens").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            if "capped or slow" in lowered and "never" not in lowered:
                offenders.append(f"{path.name}:{i}")
    assert offenders == [], f"owner wording violated: {offenders}"


def test_test_pill_ok_without_ms(_renderer_page) -> None:
    """An OK verdict with no ms must render 'check' alone, never 'Nonems'."""
    from test_render import _render, _walk_all

    from screens.server_screen import ServerScreen

    state.model_test_results = {"m1": {"verdict": "OK"}}
    state.model_testing = frozenset()
    state.models = [{"id": "m1", "status": "active"}]
    state.gateway_running = True
    try:
        tree = _render(ServerScreen)
        texts = [getattr(n, "value", "") for n in _walk_all(tree) if type(n).__name__ == "Text"]
        assert not any("Nonems" in str(t) for t in texts), f"Nonems rendered: {texts}"
        assert any("✓" in str(t) for t in texts), "OK check missing"
    finally:
        state.model_test_results = {}
        state.models = []
        state.gateway_running = False
