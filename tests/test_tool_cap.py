"""The 70-tool cap: swap-to-enable, auto-disabled overflow, defensive slice.

Every enabled tool's schema rides along on every model round, so the cap
exists in three layers: the toggle veto (swap to enable), the connect-time
overflow auto-disable (oldest enabled servers keep theirs), and the
defensive slice in _extra_tools (kani can never see more than the cap even
if settings and the hub disagree).
"""

from __future__ import annotations

from test_boot_smoke import boot_page

from core import constants
from core.state import state

__all__ = ["boot_page"]


def _fill_hub(controller, count: int) -> None:
    """Make the hub report `count` connected MCP tools without connecting."""
    controller.services.mcp.tools = [object() for _ in range(count)]


def test_toggle_on_is_vetoed_at_the_cap(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    server = controller.settings.mcp_servers[0]
    server.disabled_tools = ["toggled_tool"]
    server.enabled = True
    _fill_hub(controller, controller._mcp_tool_budget())
    assert controller._tool_count() >= constants.MAX_AGENT_TOOLS

    before = state.settings_version
    dialogs_before = len(boot_page.dialogs)
    controller.methods.toggle_mcp_tool(server.id, "toggled_tool")
    boot_page.drain()

    assert "toggled_tool" in server.disabled_tools, "veto must not enable the tool"
    assert state.settings_version == before + 1, "veto must bump so the Switch reverts"
    snacks = [str(getattr(getattr(d, "content", None), "value", "")) for d in boot_page.dialogs]
    assert len(boot_page.dialogs) > dialogs_before, "the veto must be visible"
    assert any("Tool limit reached" in s for s in snacks), snacks


def test_disable_is_allowed_at_the_cap(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    server = controller.settings.mcp_servers[0]
    server.disabled_tools = []
    server.enabled = True
    _fill_hub(controller, controller._mcp_tool_budget())

    before = state.settings_version
    controller.methods.toggle_mcp_tool(server.id, "some_tool")
    boot_page.drain()

    assert "some_tool" in server.disabled_tools, "disabling must work at the cap"
    assert state.settings_version == before + 1


def test_overflow_auto_disable_keeps_oldest_servers(boot_page) -> None:
    """A connect result over the cap disables the overflow, deterministically
    in settings order, and the second pass is stable."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    budget = controller._mcp_tool_budget()
    big = controller.settings.mcp_servers[0]
    big.enabled = True
    big.disabled_tools = []

    # 25 tools from the first (old) server, 60 from a second, over budget.
    overflow_names = [f"{big.name}.tool{i:02d}" for i in range(25)]
    late = [f"late.server.tool{i:02d}" for i in range(60)]
    names = overflow_names + late

    # Register a second enabled server so the walk has somewhere to land.
    from core.settings import MCPServerConfig

    second = MCPServerConfig(name="late", transport="streamable_http", url="https://x/mcp")
    controller.settings.mcp_servers = [big, second]

    before = state.settings_version
    controller._enforce_tool_cap(names)
    boot_page.drain()

    expected_big_kept = min(25, budget)
    assert len(big.disabled_tools) == 25 - expected_big_kept, (
        f"first server must keep {expected_big_kept}: {big.disabled_tools}"
    )
    assert len(second.disabled_tools) == 60 - max(0, budget - 25)
    assert state.settings_version > before, "enforcement must snapshot + reconnect"

    # Stability: a second pass against the trimmed connect result is a no-op.
    trimmed = [n for n in names if n not in _disabled_set(controller)]
    version_after = state.settings_version
    controller._enforce_tool_cap(trimmed[:budget])
    boot_page.drain()
    assert state.settings_version == version_after, "second pass must be a no-op"


def test_defensive_slice_keeps_kani_at_or_under_the_cap(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    _fill_hub(controller, constants.MAX_AGENT_TOOLS * 2)
    tools, generation = controller._extra_tools()
    assert len(tools) == constants.MAX_AGENT_TOOLS, f"caps must hold at assembly: got {len(tools)}"
    assert "router4" in str(generation)


def test_status_publishes_the_budget_to_the_ui(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._mcp_status(["Exa.web_search_exa"], None)
    boot_page.drain()
    assert state.mcp_tool_limit == controller._mcp_tool_budget()
    assert state.mcp_tool_limit == constants.MAX_AGENT_TOOLS - controller._builtin_tool_count()
    assert state.mcp_tools == ["Exa.web_search_exa"]


def _disabled_set(controller) -> set[str]:
    out: set[str] = set()
    for server in controller.settings.mcp_servers:
        out.update(f"{server.name}.{t}" for t in server.disabled_tools)
    return out
