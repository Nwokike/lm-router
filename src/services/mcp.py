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
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from kani.mcp import tools_from_mcp_servers
from mcp.client.session_group import (
    SseServerParameters,
    StdioServerParameters,
    StreamableHttpParameters,
)
from mcp.client.stdio import get_default_environment

from core.logging import LOG
from core.settings import AppSettings, MCPServerConfig

# Hard ceiling on one connect attempt. The MCP SDK defaults are 300s for SSE
# reads and effectively unbounded for a stdio child that never answers.
CONNECT_TIMEOUT = 20.0
# One respawn of a dead stdio child, purely to read its stderr (diagnosis
# only, failure path only).
DIAGNOSE_TIMEOUT = 5.0


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
        # `headers` is HTTP auth and must NOT be reinterpreted as env vars
        # (it once leaked an Authorization header into the child's env); the
        # env field is the sanctioned channel. The SDK's default env is a
        # safe PATH/TEMP/HOME whitelist; user vars merge on top so the
        # launcher still resolves while the server gets what it asked for.
        env = None
        if server.env:
            env = {
                **get_default_environment(),
                **{str(k): str(v) for k, v in server.env.items()},
            }
        return StdioServerParameters(
            command=_preflight_command(server),
            args=list(server.args),
            env=env,
            cwd=str(server.cwd) if server.cwd else None,
        )
    url = str(server.url) if server.url else None
    if not url:
        raise MCPError("config", "remote server needs a url")
    headers = dict(server.headers) or None
    if server.transport == "sse":
        # SSE param timeouts are plain seconds.
        kwargs: dict = {"url": url, "headers": headers}
        if server.timeout is not None:
            kwargs["timeout"] = float(server.timeout)
        if server.sse_read_timeout is not None:
            kwargs["sse_read_timeout"] = float(server.sse_read_timeout)
        return SseServerParameters(**kwargs)
    # Streamable-HTTP param timeouts are timedeltas.
    kwargs = {"url": url, "headers": headers}
    if server.timeout is not None:
        kwargs["timeout"] = timedelta(seconds=server.timeout)
    if server.sse_read_timeout is not None:
        kwargs["sse_read_timeout"] = timedelta(seconds=server.sse_read_timeout)
    return StreamableHttpParameters(**kwargs)


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


def split_stdio_command(target: str) -> tuple[str, list[str]]:
    """Split a pasted command line into command + args (Settings add form).

    Every MCP README hands users one line (`npx -y <package>`), but the form
    stored the whole line as the bare command with empty args, so any server
    that takes arguments could never start. posix=False keeps Windows
    backslashes intact; matching quote pairs are stripped per token so a
    path with spaces survives when quoted.
    """
    try:
        parts = shlex.split(str(target), posix=False)
    except ValueError:
        raise ValueError("Quotes are unbalanced in your command.") from None
    tokens = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    if not tokens:
        raise ValueError("Enter a command, for example: npx -y package-name")
    return tokens[0], tokens[1:]


def _preflight_command(server: MCPServerConfig) -> str:
    """Fail fast, and legibly, when the stdio launcher is not installed.

    Spawning a missing executable surfaces as a bare FileNotFoundError deep
    inside the MCP task group, which reached the user as "Server unreachable"
    and left them guessing. Checking first turns that into an actionable
    message, and costs one PATH lookup.

    Returns the RESOLVED command path. On Windows a typed `npx` resolves to
    npx.CMD, and CreateProcess does not apply PATHEXT itself: spawning the
    typed name fails with WinError 2 even though the launcher is installed,
    so the resolved path is what must reach the SDK.
    """
    command = str(server.command or "").strip()
    # A bare path is used as-is; a bare name is resolved against PATH.
    if not command or any(ch in command for ch in "/\\"):
        return command
    resolved = shutil.which(command)
    if resolved:
        return resolved
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


def _stderr_tail(data: object, limit: int = 240) -> str:
    """Collapse a child process's stderr to one snack-sized line."""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    elif isinstance(data, str):
        text = data
    else:
        return ""
    return " ".join(text.split())[-limit:]


async def _stdio_diagnose(server: MCPServerConfig, error: MCPError) -> str:
    """When a stdio child dies during connect, ask it why.

    "Connection closed" names no fix, while the child's own output does:
    `error: unexpected argument '-y'`, a Python traceback, a missing script.
    Respawn the server once with stdin closed (failure path only, bounded)
    and append its stderr tail. Remote servers are untouched, and any
    problem during diagnosis falls back to the original error text.
    """
    if server.transport != "stdio" or error.kind == "config":
        return str(error)
    command = str(server.command or "")
    cmd = shutil.which(command) or command
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [cmd, *server.args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DIAGNOSE_TIMEOUT,
        )
        tail = _stderr_tail(proc.stderr) or _stderr_tail(proc.stdout)
    except subprocess.TimeoutExpired as exc:
        tail = _stderr_tail(exc.stderr) or _stderr_tail(exc.stdout)
    except Exception:
        tail = ""
    if not tail:
        return str(error)
    return f"{str(error).rstrip('.')}. Server output: {tail}"


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
    # async CMs entered by THIS task (kani tools_from_mcp_servers), one per server
    _contexts: list = field(default_factory=list)
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
        try:
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
        finally:
            # Cancellation (portal teardown) must still unwind the entered
            # contexts: they were entered MANUALLY, so they sit on no stack
            # frame and task cancellation would otherwise abandon the session
            # groups and any stdio child processes.
            try:
                await self._close_owned()
            except Exception as exc:
                LOG.warning("mcp owner cleanup failed: %s", exc)

    async def _connect(self) -> None:
        """Connect each enabled server on its own.

        Three real bugs are fixed by not connecting them as one group:

        1. Tool names were prefixed with the server-REPORTED name
           (`info.name`) while the disable list and the tools dialog use the
           name the USER typed. When those differ, disabling a tool silently
           did nothing and the dialog showed no tools at all.
        2. One unreachable server failed the whole sequential group, so every
           healthy server's tools disappeared with it.
        3. A failed connect still reported "connected" downstream.
        """
        await self._close_owned()
        servers = [s for s in self.settings.mcp_servers if s.enabled]
        if not servers:
            self.generation += 1
            return

        all_tools: list = []
        all_names: list[str] = []
        # Entered in THIS task, so exiting here is legal (the cross-task
        # cancel-scope brick the header warns about).
        contexts: list = []
        failed: list[str] = []
        blocked_count = 0

        for server in servers:
            try:
                params = build_server_params(server, is_mobile=self.is_mobile)
            except MCPError as exc:
                # e.g. a stdio server on a phone, or a missing uvx binary.
                LOG.warning("mcp %s skipped: %s", server.name, exc)
                failed.append(f"{server.name}: {exc}")
                continue

            blocked = [f"{server.name}.{tool}" for tool in server.disabled_tools]
            blocked_count += len(blocked)
            context = tools_from_mcp_servers(
                [params],
                blocked_tools=blocked or None,
                # Prefix with the name the user configured, so the disable
                # list and the tools dialog agree with what is exposed.
                component_name_hook=lambda name, _info, label=server.name: f"{label}.{name}",
            )
            entered_at = time.monotonic()
            try:
                tools = await asyncio.wait_for(context.__aenter__(), timeout=CONNECT_TIMEOUT)
            except TimeoutError:
                LOG.warning(
                    "mcp %s timed out after %ss; other servers still load",
                    server.name,
                    CONNECT_TIMEOUT,
                )
                failed.append(f"{server.name}: timed out (check your connection)")
                continue
            except asyncio.CancelledError as exc:
                # Observed with slow remote servers: wait_for's timeout
                # cancel makes the SDK's anyio scope unwind as a RAW
                # CancelledError that never converts to TimeoutError. It
                # escaped every handler and killed the owner task, taking
                # MCP down for the whole session. Order matters:
                # stop request propagates; around OUR deadline it is our
                # timeout; a FAST failure (DNS typo, refused port) also
                # unwinds as a raw cancel, and anyio tags those — report
                # them as connect failures and keep the owner alive. Only
                # an untagged cancel is portal teardown (serve's finally
                # runs). task.cancelling() cannot discriminate: the broken
                # unwind skips anyio's uncancel, leaving the counter bumped.
                if self._stop.is_set():
                    raise
                if time.monotonic() - entered_at >= CONNECT_TIMEOUT - 0.5:
                    LOG.warning(
                        "mcp %s connect cancelled after %ss; other servers still load",
                        server.name,
                        CONNECT_TIMEOUT,
                    )
                    failed.append(f"{server.name}: timed out (check your connection)")
                    continue
                if "cancel scope" not in str(exc):
                    raise
                error = (
                    MCPError("tool", "Connection closed")
                    if server.transport == "stdio"
                    else MCPError("unreachable", "Server unreachable. Check the URL or command.")
                )
                detail = await _stdio_diagnose(server, error)
                LOG.warning("mcp %s failed during connect: %s", server.name, detail)
                failed.append(f"{server.name}: {detail[:140]}")
                continue
            except Exception as exc:
                # Classify, don't dump: the status snack is the user's only
                # clue, so it must name the FIX (URL, headers, protocol), not
                # just the exception (the kinds come from classify_exception,
                # which unwraps TaskGroup wrappers to the real cause).
                error = classify_exception(exc)
                detail = await _stdio_diagnose(server, error)
                LOG.warning("mcp %s failed (%s): %s", server.name, error.kind, detail)
                failed.append(f"{server.name}: {detail[:140]}")
                continue
            contexts.append(context)
            all_tools.extend(tools)
            all_names.extend(str(getattr(t, "name", "?")) for t in tools)

        # Remote servers are untrusted: cap what one can push into EVERY
        # future request (a huge tool description is both a token bomb and a
        # prompt-injection surface).
        for tool in all_tools:
            desc = getattr(tool, "desc", None)
            if isinstance(desc, str) and len(desc) > 4000:
                tool.desc = desc[:4000] + "...(truncated)"
        self._contexts = contexts
        self.tools = all_tools
        self.names = all_names
        self.generation += 1
        LOG.info(
            "mcp connected: %d tools from %d/%d servers (%d blocked)",
            len(all_tools),
            len(contexts),
            len(servers),
            blocked_count,
        )
        if failed:
            # Honest partial status rather than a silent "all good".
            self._status("Some MCP servers unavailable: " + "; ".join(failed)[:300])
        else:
            self._status(None)

    async def _close_owned(self) -> None:
        """Exit every context — MUST run in the same task that entered them."""
        contexts, self._contexts = self._contexts, []
        for context in contexts:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                LOG.warning("mcp disconnect: %s", exc)
        self._contexts = []
        self.tools = []
        self.names = []
        self.generation += 1

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
        except asyncio.CancelledError as exc:
            # A fast handshake failure (DNS typo, refused port) makes the
            # SDK's anyio scopes unwind as a RAW CancelledError that skips
            # `except Exception` and reaches the caller as cancel noise
            # instead of naming the fix. anyio tags its own cancels; a real
            # teardown or caller-side timeout cancel is untagged and must
            # propagate untouched.
            if "cancel scope" not in str(exc):
                raise
            base = (
                MCPError("tool", "Connection closed")
                if server.transport == "stdio"
                else MCPError("unreachable", "Server unreachable. Check the URL or command.")
            )
            raise MCPError(base.kind, await _stdio_diagnose(server, base)) from exc
        except Exception as exc:
            error = classify_exception(exc)
            # stdio: let the dead child explain itself (its stderr says what
            # broke; "Connection closed" alone leaves the user guessing).
            raise MCPError(error.kind, await _stdio_diagnose(server, error)) from exc
