"""Tools surface: connecting flag, Tools rename, Settings connecting copy."""

from __future__ import annotations

from test_boot_smoke import boot_page
from test_render import _render, _renderer_page, _walk_all

__all__ = ["_renderer_page", "boot_page"]


def test_connecting_flag_set_and_cleared(boot_page) -> None:
    from core.state import state
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._reapply_mcp()
    assert state.mcp_connecting is True
    controller._mcp_status(["Exa.x"], None)
    boot_page.drain()
    assert state.mcp_connecting is False
    assert state.mcp_tools == ["Exa.x"]


def test_pill_shows_connecting(_renderer_page) -> None:
    from components.chat_controls import SessionBar
    from core.state import state
    from state.controller_ctx import ControllerMethods

    state.mcp_tools = []
    state.mcp_connecting = True
    try:
        tree = _render(lambda: SessionBar(state=state, methods=ControllerMethods(), is_dark=False))
        texts = [
            str(getattr(n, "value", "")) for n in _walk_all(tree) if type(n).__name__ == "Text"
        ]
        blob = " ".join(texts)
        assert "Tools" in blob, blob[:400]
        assert "MCP" not in blob, f"pill still says MCP: {blob[:400]}"
    finally:
        state.mcp_connecting = False


def test_no_mcp_strings_on_chat_path() -> None:
    from pathlib import Path

    chat_controls = (
        Path(__file__).resolve().parent.parent / "src" / "components" / "chat_controls.py"
    ).read_text(encoding="utf-8")
    for line in chat_controls.splitlines():
        code = line.split("#", 1)[0]
        assert '"MCP' not in code and "'MCP" not in code, (
            f"chat pill still says MCP: {line.strip()}"
        )
