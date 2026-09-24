"""MCP hub: user-registered servers become kani tools.

Enabled stdio/remote servers are connected ONCE and held open for the app
lifetime. The session context is owned by ONE long-lived task (serve()): anyio
cancel scopes MUST be entered and exited in the same task — closing from a
later portal task raises "Attempted to exit cancel scope in a different task",
which unwinds the portal's task group and permanently bricks the agent
(every later call raises "This portal is not running"). Callers only signal
via threading events; errors and tool schemas are classified for the UI.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from kani.mcp import tools_from_mcp_servers
from mcp.client.session_group import (
    SseServerParameters,
    StdioServerParameters,
    StreamableHttpParameters,
)

from core.logging import LOG
from core.settings import AppSettings, MCPServerConfig

# Hard ceiling on one connect attempt. The MCP SDK defaults are 300s for SSE
# reads and effectively unbounded for a stdio child that never answers.
CONNECT_TIMEOUT = 20.0


def _schema_problem(schema: object) -> tuple[bool, str | None]:
    """Minimal JSON-Schema sanity check for a tool's parameter schema."""
    if not isinstance(schema, dict):
        return (False, "schema is not a JSON object")
    kind = schema.get("type")
    if kind is not None and kind not in (
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
        "null",
    ):
        return (False, f"unknown schema type {kind!r}")
    if isinstance(schema.get("properties"), dict) and kind not in (None, "object"):
        return (False, "properties requires type 'object'")
    return (True, None)


class MCPError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # unreachable | protocol | auth | tool | config | schema


def build_server_params(server: MCPServerConfig, is_mobile: bool = False) -> object:
    if server.transport == "stdio":
        if is_mobile:
            raise MCPError(
                "config",
                "stdio transport is not supported on mobile devices. Use streamable_http or sse.",
            )
        if not server.command:
            raise MCPError("config", "stdio server needs a command")
        _preflight_command(server)
        env = dict(server.headers) if server.headers else None
        return StdioServerParameters(command=server.command, args=list(server.args), env=env)
    url = str(server.url) if server.url else None
    if not url:
        raise MCPError("config", "remote server needs a url")
    if server.transport == "sse":
        return SseServerParameters(url=url, headers=dict(server.headers) or None)
    return StreamableHttpParameters(url=url, headers=dict(server.headers) or None)


# How a user fixes a missing launcher. Covers the runners people actually type
# into an MCP config, so the error can say what to do rather than just "not found".
_LAUNCHER_HINTS = {
    "uvx": "Install uv (https://docs.astral.sh/uv/) so `uvx` is on your PATH, "
    "or add a streamable_http server instead.",
    "uv": "Install uv (https://docs.astral.sh/uv/) or add a streamable_http server instead.",
    "npx": "Install Node.js so `npx` is on your PATH, or add a streamable_http server instead.",
    "node": "Install Node.js so `node` is on your PATH, or add a streamable_http server instead.",
    "deno": "Install Deno, or add a streamable_http server instead.",
    "bunx": "Install Bun, or add a streamable_http server instead.",
}


def _preflight_command(server: MCPServerConfig) -> None:
    """Fail fast, and legibly, when the stdio launcher is not installed.

    Spawning a missing executable surfaces as a bare FileNotFoundError deep
    inside the MCP task group, which reached the user as "Server unreachable"
    and left them guessing. Checking first turns that into an actionable
    message, and costs one PATH lookup.
    """
    command = str(server.command or "").strip()
    # A bare path is used as-is; a bare name is resolved against PATH.
    if not command or any(ch in command for ch in "/\\"):
        return
    if shutil.which(command):
        return
    hint = _LAUNCHER_HINTS.get(
        command.lower(),
        f"Make sure `{command}` is installed and on your PATH.",
    )
    raise MCPError(
        "config",
        f"`{command}` was not found on this device, so the {server.name!r} MCP "
        f"server cannot start. {hint}",
    )


def enabled_params(settings: AppSettings, is_mobile: bool = False) -> list[object]:
    params = []
    for server in settings.mcp_servers:
        if server.enabled:
            params.append(build_server_params(server, is_mobile=is_mobile))
    return params


def classify_exception(exc: Exception) -> MCPError:
    text = str(exc)
    if isinstance(exc, MCPError):
        return exc
    # anyio/asyncio wrap transport failures in a TaskGroup ExceptionGroup —
    # classify the real cause, not the wrapper text (audit: SSE refusals
    # surfaced as kind=tool "unhandled errors in a TaskGroup").
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return classify_exception(exc.exceptions[0])
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return MCPError("unreachable", "Server unreachable. Check the URL or command.")
    lowered = text.lower()
    if "unauthorized" in lowered or "401" in text or "403" in text:
        return MCPError("auth", "Authentication failed. Check the headers or key.")
    if (
        "protocol" in lowered
        or "-32601" in text
        or "-32602" in text
        or "unsupported protocol version" in lowered
    ):
        return MCPError("protocol", "Server speaks an incompatible MCP protocol version.")
    return MCPError("tool", text[:400])


@dataclass
class MCPHub:
    settings: AppSettings
    # Phones have no subprocess model, so a stdio server (uvx/npx/python ...)
    # can never work there. Captured once on the Flet thread at construction
    # and threaded into every params build; the guard used to default to
    # False at every call site, so it never fired.
    is_mobile: bool = False
    tools: list = field(default_factory=list)  # AIFunction list for kani
    names: list[str] = field(default_factory=list)
    generation: int = 0
    on_status: Callable[[list[str] | None, str | None], None] | None = None
    _context: object | None = None  # entered async CM (kani tools_from_mcp_servers)
    _reconnect: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)

    def request_reconnect(self) -> None:
        """Thread-safe: wake the owner task to (re)connect the enabled set."""
        self._reconnect.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._reconnect.set()

    def _status(self, error: str | None) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(list(self.names), error)
        except Exception as exc:
            LOG.warning("mcp status callback failed: %s", exc)

    async def serve(self) -> None:
        """Owner task: enter/close the tools context in THIS task only."""
        while not self._stop.is_set():
            try:
                await self._connect()
            except Exception as exc:
                LOG.warning("mcp apply failed: %s", exc)
                self.tools = []
                self.names = []
                self.generation += 1
                self._status(f"MCP connection failed: {str(exc)[:200]}")
            else:
                self._status(None)
            self._reconnect.clear()
            while not self._stop.is_set() and not self._reconnect.is_set():
                # thread-safe wait; never blocks the portal loop
                await asyncio.to_thread(self._reconnect.wait, 0.5)
        await self._close_owned()

    async def _connect(self) -> None:
        await self._close_owned()
        params = enabled_params(self.settings, is_mobile=self.is_mobile)
        if not params:
            self.generation += 1
            return
        blocked: list[str] = []
        for server in self.settings.mcp_servers:
            if server.enabled and server.disabled_tools:
                for dt in server.disabled_tools:
                    blocked.append(f"{server.name}.{dt}")
        context = tools_from_mcp_servers(
            params,
            blocked_tools=blocked or None,
            component_name_hook=lambda name, info: f"{info.name}.{name}",
        )
        # A stalled server must never park the owner task indefinitely: the
        # MCP defaults are 300s for SSE reads and UNBOUNDED for a stdio child
        # that never answers, and `serve()` only checks _stop/_reconnect
        # BETWEEN connects — so without this bound, Quit and "remove server"
        # hang and the whole agent portal wedges.
        try:
            tools = await asyncio.wait_for(context.__aenter__(), timeout=CONNECT_TIMEOUT)
        except TimeoutError:
            LOG.warning(
                "mcp connect timed out after %ss; continuing without MCP tools",
                CONNECT_TIMEOUT,
            )
            # The context was never entered successfully; drop it so we do not
            # try to exit an un-entered async context from this task later.
            self._context = None
            self.tools = []
            self.names = []
            self.generation += 1
            return
        except Exception as exc:
            LOG.warning("mcp connect failed: %s", exc)
            self._context = None
            self.tools = []
            self.names = []
            self.generation += 1
            return
        self._context = context
        self.tools = list(tools)
        self.names = [str(getattr(t, "name", "?")) for t in self.tools]
        self.generation += 1
        LOG.info(
            "mcp connected: %d tools from %d servers (%d blocked)",
            len(self.tools),
            len(params),
            len(blocked),
        )

    async def _close_owned(self) -> None:
        """Exit the context — MUST run in the same task that entered it."""
        context, self._context = self._context, None
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                LOG.warning("mcp disconnect: %s", exc)
        self.tools = []
        self.names = []
        self.generation += 1

    async def apply(self, params: list[object]) -> None:
        """One-shot connect for tests/tooling (same task enter+exit)."""
        await self._close_owned()
        if not params:
            self.generation += 1
            return
        blocked: list[str] = []
        for server in self.settings.mcp_servers:
            if server.enabled and server.disabled_tools:
                for dt in server.disabled_tools:
                    blocked.append(f"{server.name}.{dt}")
        context = tools_from_mcp_servers(
            params,
            blocked_tools=blocked or None,
            component_name_hook=lambda name, info: f"{info.name}.{name}",
        )
        tools = await context.__aenter__()
        self._context = context
        self.tools = list(tools)
        self.names = [str(getattr(t, "name", "?")) for t in self.tools]
        self.generation += 1
        LOG.info(
            "mcp connected: %d tools from %d servers (%d blocked)",
            len(self.tools),
            len(params),
            len(blocked),
        )

    async def close(self) -> None:
        await self._close_owned()

    async def test(self, server: MCPServerConfig) -> list[dict]:
        """Short-lived connect + list_tools with schema validation for the Settings UI."""
        try:
            params = build_server_params(server, is_mobile=self.is_mobile)
        except MCPError as exc:
            raise exc
        try:
            context = tools_from_mcp_servers([params])
            tools = await context.__aenter__()
            results: list[dict] = []
            try:
                for t in tools:
                    name = str(getattr(t, "name", "?"))
                    desc = str(getattr(t, "desc", "") or "")
                    schema = getattr(t, "json_schema", None)
                    # A tool without a usable schema cannot be called, so
                    # flag it. A local structural check is enough here: the
                    # full jsonschema package was only ever validating schemas
                    # this app itself produced.
                    schema_valid, schema_error = _schema_problem(schema)
                    results.append(
                        {
                            "name": name,
                            "description": desc,
                            "schema_valid": schema_valid,
                            "schema_error": schema_error,
                            "input_schema": schema,
                        },
                    )
                return results
            finally:
                await context.__aexit__(None, None, None)
        except Exception as exc:
            raise classify_exception(exc) from exc
