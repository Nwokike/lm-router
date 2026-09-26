"""Unit tests for MCP hub service, transport parameters, and schema validation."""

import shutil
from typing import ClassVar

import httpx
import pytest

from core.settings import AppSettings, MCPServerConfig
from services.mcp import (
    MCPError,
    MCPHub,
    build_server_params,
    classify_exception,
    enabled_params,
)


def test_build_server_params_all_transports() -> None:
    # Stdio
    stdio_cfg = MCPServerConfig(
        name="local",
        transport="stdio",
        command="npx",
        args=["-y", "test-server"],
    )
    stdio_params = build_server_params(stdio_cfg, is_mobile=False)
    # A bare launcher name is resolved before spawn: Windows `npx` must
    # become npx.CMD, because CreateProcess applies no PATHEXT and spawning
    # the typed name fails with WinError 2 even though npx is installed.
    assert getattr(stdio_params, "command", "") == (shutil.which("npx") or "npx")

    # Stdio on mobile is rejected
    with pytest.raises(MCPError) as exc_info:
        build_server_params(stdio_cfg, is_mobile=True)
    assert exc_info.value.kind == "config"
    assert "mobile" in str(exc_info.value).lower()

    # Remote SSE
    sse_cfg = MCPServerConfig(
        name="remote-sse",
        transport="sse",
        url="http://localhost:8000/sse",
    )
    sse_params = build_server_params(sse_cfg)
    assert str(getattr(sse_params, "url", "")) == "http://localhost:8000/sse"

    # Remote Streamable HTTP
    http_cfg = MCPServerConfig(
        name="remote-http",
        transport="streamable_http",
        url="http://localhost:8000/mcp",
    )
    http_params = build_server_params(http_cfg)
    assert str(getattr(http_params, "url", "")) == "http://localhost:8000/mcp"


def test_enabled_params_filters_disabled() -> None:
    s1 = MCPServerConfig(name="s1", transport="sse", url="http://s1/sse", enabled=True)
    s2 = MCPServerConfig(name="s2", transport="sse", url="http://s2/sse", enabled=False)
    settings = AppSettings(mcp_servers=[s1, s2])

    params = enabled_params(settings)
    assert len(params) == 1
    assert str(getattr(params[0], "url", "")) == "http://s1/sse"


def test_classify_exception_mappings() -> None:
    # Direct MCPError
    assert classify_exception(MCPError("custom", "test")).kind == "custom"

    # Network / Timeout
    assert classify_exception(httpx.ConnectError("refused")).kind == "unreachable"
    assert classify_exception(httpx.ReadTimeout("timed out")).kind == "unreachable"

    # Schema error
    # A tool schema problem is now detected locally (no jsonschema dep).
    from services.mcp import _schema_problem

    assert _schema_problem({"type": "nonsense"})[0] is False
    assert _schema_problem({"type": "object", "properties": {}})[0] is True
    assert _schema_problem(None)[0] is False

    # Auth
    assert classify_exception(Exception("HTTP 401 Unauthorized")).kind == "auth"
    assert classify_exception(Exception("403 Forbidden")).kind == "auth"

    # Protocol
    proto_err = Exception("Unsupported protocol version 2024-01-01")
    assert classify_exception(proto_err).kind == "protocol"
    assert classify_exception(Exception("RPC error -32601 Method not found")).kind == "protocol"

    # TaskGroup ExceptionGroups (anyio) must unwrap to the real cause
    group = ExceptionGroup("taskgroup", [httpx.ConnectError("refused")])
    assert classify_exception(group).kind == "unreachable"

    # Generic tool error
    assert classify_exception(Exception("Calculation error")).kind == "tool"


@pytest.mark.anyio
async def test_hub_test_schema_validation(monkeypatch) -> None:
    class FakeToolValid:
        name = "valid_tool"
        desc = "Tool with a valid schema"
        json_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }

    class FakeToolInvalid:
        name = "broken_tool"
        desc = "Tool with an invalid schema"
        json_schema: ClassVar[dict] = {"type": "not-a-valid-type"}

    class FakeContext:
        async def __aenter__(self):
            return [FakeToolValid(), FakeToolInvalid()]

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(
        "services.mcp.tools_from_mcp_servers",
        lambda params, **kw: FakeContext(),
    )

    hub = MCPHub(AppSettings())
    server = MCPServerConfig(name="test", transport="sse", url="http://test/sse")
    results = await hub.test(server)

    assert len(results) == 2
    valid_res = next(r for r in results if r["name"] == "valid_tool")
    assert valid_res["schema_valid"] is True
    assert valid_res["schema_error"] is None

    broken_res = next(r for r in results if r["name"] == "broken_tool")
    assert broken_res["schema_valid"] is False
    assert broken_res["schema_error"] is not None


@pytest.mark.anyio
async def test_connect_passes_blocked_tools(monkeypatch) -> None:
    """Per-server connect (the SHIPPED path) must pass the configured
    server's disabled tools. The old test drove apply(), a test-only fork
    that reintroduced all three bugs _connect was written to fix — so the
    reconnect path stayed effectively untested."""
    passed_blocked: list = []

    class FakeContext:
        async def __aenter__(self):
            return []

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_tools_from_mcp_servers(params, blocked_tools=None, **kwargs):
        if blocked_tools:
            passed_blocked.extend(blocked_tools)
        return FakeContext()

    monkeypatch.setattr("services.mcp.tools_from_mcp_servers", mock_tools_from_mcp_servers)

    server = MCPServerConfig(
        name="github",
        transport="sse",
        url="http://github/sse",
        disabled_tools=["delete_repo", "push_file"],
    )
    settings = AppSettings(mcp_servers=[server])
    hub = MCPHub(settings)
    try:
        await hub._connect()
        assert "github.delete_repo" in passed_blocked
        assert "github.push_file" in passed_blocked
    finally:
        await hub._close_owned()


@pytest.mark.anyio
async def test_a_stalled_server_cannot_wedge_the_portal(monkeypatch) -> None:
    """A server that never answers must not park the MCP owner task.

    The SDK defaults are 300s for SSE reads and unbounded for a wedged stdio
    child; `serve()` only checks _stop/_reconnect BETWEEN connects, so without
    a bound, Quit and "remove server" hung and the agent portal never recovered.
    """
    import asyncio

    from services import mcp as mcp_mod

    class _HangingContext:
        async def __aenter__(self):
            await asyncio.sleep(3600)  # never returns
            return []

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(mcp_mod, "tools_from_mcp_servers", lambda *a, **k: _HangingContext())
    monkeypatch.setattr(mcp_mod, "CONNECT_TIMEOUT", 0.2)

    settings = AppSettings(
        mcp_servers=[
            MCPServerConfig(
                name="stuck",
                transport="streamable_http",
                url="https://never-answers.invalid/mcp",
            ),
        ],
    )
    statuses: list = []
    hub = MCPHub(settings, on_status=lambda names, error: statuses.append((names, error)))

    await hub._connect()

    # Bounded, and the hub is left in a clean, usable state.
    assert hub.tools == []
    assert hub.names == []
    assert hub._contexts == []


def test_stdio_servers_are_refused_on_mobile() -> None:
    """A phone has no subprocess model, so `uvx`/`npx` servers can never run.

    The guard existed but every call site used the default `is_mobile=False`,
    so it never fired and the app would hang trying to spawn a process.
    """
    from services.mcp import build_server_params, enabled_params

    stdio = MCPServerConfig(name="s", transport="stdio", command="uvx", args=["some-mcp"])

    # Desktop: fine.
    assert build_server_params(stdio, is_mobile=False) is not None

    # Mobile: refused with an actionable message.
    with pytest.raises(MCPError) as exc:
        build_server_params(stdio, is_mobile=True)
    assert "mobile" in str(exc.value).lower()

    # And the bulk path refuses it too, rather than silently spawning.
    settings = AppSettings(mcp_servers=[stdio])
    with pytest.raises(MCPError):
        enabled_params(settings, is_mobile=True)

    # Remote transports still work on mobile.
    remote = MCPServerConfig(name="r", transport="streamable_http", url="https://x.dev/mcp")
    assert build_server_params(remote, is_mobile=True) is not None


def test_missing_stdio_launcher_is_reported_actionably(monkeypatch) -> None:
    """A desktop user without `uv` must get a fix, not a bare ENOENT.

    Spawning a missing executable used to surface as FileNotFoundError deep
    inside the MCP task group, which the user saw as "Server unreachable".
    """
    from services.mcp import build_server_params

    monkeypatch.setattr("services.mcp.shutil.which", lambda _cmd: None)
    server = MCPServerConfig(name="docs", transport="stdio", command="uvx", args=["mcp-docs"])

    with pytest.raises(MCPError) as exc:
        build_server_params(server, is_mobile=False)
    message = str(exc.value)
    assert "uvx" in message
    assert "not found" in message.lower()
    # It must say what to do, not just that it broke.
    assert "install" in message.lower() or "PATH" in message


def test_present_stdio_launcher_is_accepted(monkeypatch) -> None:
    from services.mcp import build_server_params

    monkeypatch.setattr("services.mcp.shutil.which", lambda _cmd: "/usr/bin/uvx")
    server = MCPServerConfig(name="docs", transport="stdio", command="uvx", args=[])
    assert build_server_params(server) is not None


def test_explicit_path_is_not_path_resolved(monkeypatch) -> None:
    """A full path or script is used verbatim; we do not second-guess it."""
    from services.mcp import build_server_params

    def _boom(_cmd):
        raise AssertionError("should not resolve an explicit path")

    monkeypatch.setattr("services.mcp.shutil.which", _boom)
    server = MCPServerConfig(name="s", transport="stdio", command="/opt/bin/my-server", args=[])
    assert build_server_params(server) is not None


@pytest.mark.anyio
async def test_one_bad_server_does_not_hide_the_healthy_ones(monkeypatch) -> None:
    """A single unreachable server must not blank every other server's tools."""
    import contextlib

    from services import mcp as mcp_mod

    class _GoodContext:
        async def __aenter__(self):
            class _T:
                name = "good_tool"

            return [_T()]

        async def __aexit__(self, *exc):
            return None

    class _BadContext:
        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc):
            return None

    def fake_tools(params, **kwargs):
        # params[0] carries the url/command we can key on
        first = params[0]
        label = getattr(first, "url", None) or getattr(first, "command", "") or ""
        return _GoodContext() if "good" in str(label) else _BadContext()

    monkeypatch.setattr(mcp_mod, "tools_from_mcp_servers", fake_tools)
    monkeypatch.setattr(mcp_mod, "CONNECT_TIMEOUT", 5.0)

    settings = AppSettings(
        mcp_servers=[
            MCPServerConfig(name="bad", transport="streamable_http", url="https://bad.invalid/mcp"),
            MCPServerConfig(name="good", transport="streamable_http", url="https://good.dev/mcp"),
        ],
    )
    statuses: list = []
    hub = MCPHub(settings, on_status=lambda names, err: statuses.append((names, err)))
    try:
        await hub._connect()
        # Read BEFORE cleanup: _close_owned() resets tools/names by design.
        names = list(hub.names)
    finally:
        with contextlib.suppress(Exception):
            await hub._close_owned()

    # The healthy server's tool survived the bad one.
    assert any("good_tool" in n for n in names), names
    # ...and the user was told, rather than being shown a false success.
    assert any(err for _names, err in statuses if err)


@pytest.mark.anyio
async def test_tool_names_use_the_configured_server_name(monkeypatch) -> None:
    """The disable list and tools dialog key off the USER's server name."""
    import contextlib

    from services import mcp as mcp_mod

    seen_prefixes: list[str] = []

    class _Ctx:
        async def __aenter__(self):
            return []

        async def __aexit__(self, *exc):
            return None

    def fake_tools(params, **kwargs):
        hook = kwargs.get("component_name_hook")
        if hook:
            # Mimic kani: the server may report a different info.name.
            class _Info:
                name = "reported-by-server"

            seen_prefixes.append(hook("some_tool", _Info()))
        return _Ctx()

    monkeypatch.setattr(mcp_mod, "tools_from_mcp_servers", fake_tools)
    settings = AppSettings(
        mcp_servers=[
            MCPServerConfig(name="my-docs", transport="streamable_http", url="https://x.dev/mcp"),
        ],
    )
    hub = MCPHub(settings)
    try:
        await hub._connect()
    finally:
        with contextlib.suppress(Exception):
            await hub._close_owned()

    # Our name wins, so disabled_tools=["some_tool"] actually matches.
    assert seen_prefixes == ["my-docs.some_tool"]


@pytest.mark.anyio
async def test_serve_handles_empty_config_and_stops_cleanly() -> None:
    """serve() is now spawned on EVERY boot, so it must tolerate no servers.

    The owner loop with an empty config short-circuits in _connect (one
    generation bump, then parks on the reconnect wait) and must exit through
    _close_owned() once stopped — this is the path the unconditional boot
    spawn exercises on a fresh install.
    """
    statuses: list = []
    hub = MCPHub(AppSettings(), is_mobile=False, on_status=lambda *a: statuses.append(a))

    import asyncio

    task = asyncio.ensure_future(hub.serve())
    await asyncio.sleep(0.3)
    assert hub.generation >= 1, "empty config must still complete a connect pass"
    assert hub.tools == []

    hub.request_stop()
    await asyncio.wait_for(task, timeout=5)
    # Owner exited through its cleanup path with nothing leaked.
    assert hub._contexts == []


@pytest.mark.anyio
async def test_serve_cancellation_still_closes_contexts(monkeypatch) -> None:
    """serve()'s finally must unwind entered contexts on cancellation.

    The contexts are entered MANUALLY (stored in _contexts), so they sit on
    no stack frame — without the finally, a portal teardown mid-wait
    abandoned every session group and any stdio child process.
    """
    import asyncio
    import contextlib

    hub = MCPHub(AppSettings(), is_mobile=False, on_status=lambda *a: None)
    closed = {"n": 0}

    @contextlib.asynccontextmanager
    async def _spy():
        try:
            yield ["spy-tool"]
        finally:
            closed["n"] += 1

    async def _fake_connect() -> None:
        context = _spy()
        await context.__aenter__()
        hub._contexts.append(context)
        hub.generation += 1

    monkeypatch.setattr(hub, "_connect", _fake_connect)
    task = asyncio.ensure_future(hub.serve())
    await asyncio.sleep(0.2)  # connect, then park on the reconnect wait
    assert hub._contexts, "precondition: a context is entered"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert closed["n"] == 1, "cancellation must still run the owner's cleanup"
    assert hub._contexts == []


@pytest.mark.anyio
async def test_slow_connect_cancel_does_not_kill_the_owner(monkeypatch) -> None:
    """A connect that outlives CONNECT_TIMEOUT must land as 'timed out' —
    not as an escaping CancelledError that kills the owner task and takes
    MCP down for the session (live bug: slow remote servers did exactly
    this, unwinding the SDK's anyio scope past every handler)."""
    import asyncio

    from services import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "CONNECT_TIMEOUT", 0.2)
    statuses: list = []

    class _SlowContext:
        async def __aenter__(self):
            await asyncio.sleep(5.0)
            return []

        async def __aexit__(self, *exc):
            pass

    class _FastContext:
        async def __aenter__(self):
            return []

        async def __aexit__(self, *exc):
            pass

    contexts = iter([_SlowContext(), _FastContext()])
    monkeypatch.setattr(
        "services.mcp.tools_from_mcp_servers",
        lambda *a, **k: next(contexts),
    )

    slow = MCPServerConfig(name="slow", transport="sse", url="http://slow/sse")
    fast = MCPServerConfig(name="fast", transport="sse", url="http://fast/sse")
    settings = AppSettings(mcp_servers=[slow, fast])
    hub = MCPHub(settings, on_status=lambda *a: statuses.append(a))
    try:
        await hub._connect()  # must NOT raise
        assert hub.generation >= 1, "connect completed despite the slow server"
        assert any(len(s) > 1 and s[1] and "timed out" in str(s[1]) for s in statuses), statuses
        # the fast server still connected (per-server isolation)
        assert hub._contexts, "the healthy server must survive the slow one"
    finally:
        await hub._close_owned()


@pytest.mark.anyio
async def test_dead_stdio_child_reports_its_own_stderr() -> None:
    """A server that dies on startup must say WHY in the Settings test.

    The SDK only reports "Connection closed", which names no fix; the
    diagnosis respawn captures the child's own stderr (wrong flag, crash on
    import, missing script) and appends it to the error the user reads.
    """
    import sys

    server = MCPServerConfig(
        name="bad",
        transport="stdio",
        command=sys.executable,
        args=[
            "-c",
            "import sys; sys.stderr.write('unexpected argument: -y'); sys.exit(1)",
        ],
    )
    hub = MCPHub(AppSettings())
    with pytest.raises(MCPError) as exc:
        await hub.test(server)
    assert "unexpected argument: -y" in str(exc.value)


@pytest.mark.anyio
async def test_remote_failure_keeps_the_classified_message(monkeypatch) -> None:
    """Remote failures are never respawned; the classified fix stands alone."""

    def _boom(_params, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("services.mcp.tools_from_mcp_servers", _boom)
    hub = MCPHub(AppSettings())
    server = MCPServerConfig(name="remote", transport="streamable_http", url="https://x.test/mcp")
    with pytest.raises(MCPError) as exc:
        await hub.test(server)
    assert str(exc.value) == "Server unreachable. Check the URL or command."


def test_bare_launcher_resolves_to_the_installed_path(monkeypatch) -> None:
    """The spawn must receive the resolved path, not the typed name."""
    monkeypatch.setattr(
        "services.mcp.shutil.which",
        lambda _cmd: r"C:\Program Files\nodejs\npx.CMD",
    )
    server = MCPServerConfig(name="t", transport="stdio", command="npx", args=["-y", "pkg"])
    params = build_server_params(server, is_mobile=False)
    assert params.command == r"C:\Program Files\nodejs\npx.CMD"
    assert params.args == ["-y", "pkg"]


def test_stdio_target_splits_a_pasted_command_line() -> None:
    """One-line entry, the way every MCP README writes it."""
    from services.mcp import split_stdio_command

    cmd, args = split_stdio_command("npx -y @modelcontextprotocol/server-everything")
    assert cmd == "npx"
    assert args == ["-y", "@modelcontextprotocol/server-everything"]

    cmd, args = split_stdio_command("uvx mcp-server-fetch")
    assert (cmd, args) == ("uvx", ["mcp-server-fetch"])

    # A quoted Windows path keeps its backslashes and spaces.
    cmd, args = split_stdio_command(r'"C:\Program Files\nodejs\npx.CMD" -y pkg')
    assert cmd == r"C:\Program Files\nodejs\npx.CMD"
    assert args == ["-y", "pkg"]

    # A bare path stays untouched (non-posix split: no backslash eating).
    cmd, args = split_stdio_command(r"C:\tools\my-server.exe")
    assert (cmd, args) == (r"C:\tools\my-server.exe", [])


def test_stdio_target_rejects_empty_and_unbalanced_input() -> None:
    from services.mcp import split_stdio_command

    with pytest.raises(ValueError, match="example"):
        split_stdio_command("   ")
    with pytest.raises(ValueError, match="unbalanced"):
        split_stdio_command('npx "pkg')


@pytest.mark.anyio
async def test_fast_cancel_connect_reports_instead_of_killing_the_owner(monkeypatch) -> None:
    """A DNS typo unwinds as a raw anyio cancel; the owner must survive.

    Fast handshake failures reach _connect as `CancelledError:
    'Cancelled via cancel scope ...'` with task.cancelling() bumped (the
    broken unwind skips anyio's uncancel). Treating that as portal teardown
    re-raised it, killed the owner task, and took MCP down for the session.
    """
    import asyncio

    from services import mcp as mcp_mod

    class _CancelledContext:
        async def __aenter__(self):
            raise asyncio.CancelledError("Cancelled via cancel scope deadbeef")

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(
        mcp_mod,
        "tools_from_mcp_servers",
        lambda *a, **k: _CancelledContext(),
    )
    settings = AppSettings(
        mcp_servers=[
            MCPServerConfig(
                name="typo", transport="streamable_http", url="https://nope.invalid/mcp"
            )
        ],
    )
    statuses: list = []
    hub = MCPHub(settings, on_status=lambda names, err: statuses.append((names, err)))
    try:
        await hub._connect()  # must NOT raise
        assert hub.generation >= 1, "connect did not complete"
        errors = [err for _names, err in statuses if err]
        assert errors, statuses
        # The user is told what to fix, not shown cancel noise.
        assert any("URL" in err for err in errors), errors
    finally:
        await hub._close_owned()


@pytest.mark.anyio
async def test_handshake_cancel_becomes_an_actionable_test_error(monkeypatch) -> None:
    """The Settings test button must read 'unreachable', not raw cancel noise."""
    import asyncio

    def _cancelled(_params, **_kwargs):
        class _Ctx:
            async def __aenter__(self):
                raise asyncio.CancelledError("Cancelled via cancel scope 123")

            async def __aexit__(self, *exc):
                return None

        return _Ctx()

    monkeypatch.setattr("services.mcp.tools_from_mcp_servers", _cancelled)
    hub = MCPHub(AppSettings())
    server = MCPServerConfig(name="remote", transport="streamable_http", url="https://x.test/mcp")
    with pytest.raises(MCPError) as exc:
        await hub.test(server)
    assert exc.value.kind == "unreachable"
    assert "URL" in str(exc.value)


@pytest.mark.anyio
async def test_untagged_cancellation_still_propagates(monkeypatch) -> None:
    """A real teardown/timeout cancel is untagged and must pass through."""
    import asyncio

    def _cancelled(_params, **_kwargs):
        class _Ctx:
            async def __aenter__(self):
                raise asyncio.CancelledError()  # no anyio tag: external cancel

            async def __aexit__(self, *exc):
                return None

        return _Ctx()

    monkeypatch.setattr("services.mcp.tools_from_mcp_servers", _cancelled)
    hub = MCPHub(AppSettings())
    server = MCPServerConfig(name="remote", transport="streamable_http", url="https://x.test/mcp")
    with pytest.raises(asyncio.CancelledError):
        await hub.test(server)


def test_build_passes_the_full_sdk_surface() -> None:
    """Our UI must not be the layer that limits a server configuration:
    env and cwd (stdio) and both remote timeouts are the SDK's own fields
    and must reach it intact."""
    import os
    from datetime import timedelta

    stdio = MCPServerConfig(
        name="s",
        transport="stdio",
        command="npx",
        args=["-y", "pkg"],
        env={"API_KEY": "secret"},
        cwd=r"C:\work",
    )
    params = build_server_params(stdio, is_mobile=False)
    assert params.args == ["-y", "pkg"]
    assert params.env is not None, "env must reach the SDK, not be dropped"
    assert params.env["API_KEY"] == "secret"
    # The SDK's safe whitelist must survive the merge, or the launcher
    # itself stops resolving (no PATH for the child).
    if os.environ.get("PATH"):
        assert params.env.get("PATH"), "PATH must survive the env merge"
    assert params.cwd == r"C:\work"

    http = MCPServerConfig(
        name="h",
        transport="streamable_http",
        url="https://x.test/mcp",
        timeout=12.5,
        sse_read_timeout=99.0,
    )
    http_params = build_server_params(http)
    assert http_params.timeout == timedelta(seconds=12.5)
    assert http_params.sse_read_timeout == timedelta(seconds=99.0)

    sse = MCPServerConfig(name="q", transport="sse", url="https://x.test/sse", timeout=7)
    sse_params = build_server_params(sse)
    assert sse_params.timeout == 7  # SSE timeouts are plain seconds
    assert sse_params.sse_read_timeout == 300  # untouched: SDK default stands


def test_timeouts_must_be_positive() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="positive"):
        MCPServerConfig(name="x", transport="sse", url="https://x.test/sse", timeout=0)
    with pytest.raises(ValidationError, match="positive"):
        MCPServerConfig(
            name="y",
            transport="streamable_http",
            url="https://x.test/mcp",
            sse_read_timeout=-1,
        )
