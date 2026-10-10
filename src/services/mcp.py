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
import contextlib
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

MCP_DESC_CAP = 2000
MCP_RESULT_CAP = 6000

# The MCP SDK's ClientSessionGroup._establish_session cleans up with
# `except Exception` — CANCELLED (our connect timeout) skips it, the
# half-open stdio/SSE/streamable client generator stays suspended, and its
# later cross-task GC finalization cancels random tasks' cancel scopes
# (observed: the agent portal itself died with "cancel scope that isn't
# the current tasks's current"). Track every client the SDK opens so WE
# can close it in the task that entered it, immediately, deterministically.
_PENDING_CLIENTS: list = []


def _extract_session(tool) -> tuple[object | None, str]:
    """The (ClientSessionGroup, tool name) a kani MCP tool closes over.

    kani builds `AIFunction(partial(_call_mcp_tool, name))` where the closed
    over `grp` is the live session group. We need both to rebuild the tool
    body; anything unrecognisable (test stubs, a future kani shape) returns
    (None, "") and the tool keeps kani's own body.
    """
    inner = getattr(tool, "inner", None)
    wrapped = getattr(tool, "__wrapped__", None) or getattr(inner, "__wrapped__", None)
    if wrapped is None:
        return None, ""
    func = getattr(wrapped, "func", None)
    args = getattr(wrapped, "args", None) or (getattr(tool, "name", ""),)
    name = str(args[0]) if args else ""
    closure = getattr(func, "__closure__", None) or ()
    for cell in closure:
        candidate = getattr(cell, "cell_contents", None)
        if candidate is not None and hasattr(candidate, "call_tool"):
            return candidate, name
    return None, ""


def _mcp_tool_body(grp: object, name: str):
    """A tool body that handles every content block kani rejects.

    kani's wrapper only understands text/image/audio and RAISES on
    EmbeddedResource — which is exactly what file/docs servers (GitHub's
    get_file_contents returns the file as a text resource) send back. This
    renders resources as "[uri]\\n<text>", notes media it cannot inline, and
    never raises: a tool error costs the model a whole turn.
    """

    async def _body(**kwargs) -> str:
        result = await grp.call_tool(name, arguments=kwargs)  # type: ignore[attr-defined]
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            kind = str(getattr(block, "type", "") or "")
            if kind == "text":
                parts.append(str(getattr(block, "text", "") or ""))
            elif kind == "resource":
                resource = getattr(block, "resource", None)
                text = str(getattr(resource, "text", "") or "")
                uri = str(getattr(resource, "uri", "") or "")
                if not text:
                    continue
                parts.append(f"[{uri}]\n{text}" if uri else text)
            else:
                # Images/audio need multimodal parts the transcript cannot
                # carry text-only; a note beats failing the whole call.
                parts.append(f"[{kind or 'unknown'} content omitted]")
        return "\n\n".join(p for p in parts if p) or "(no content)"

    return _body


def _harden_mcp_tools(tools: list) -> list:
    """Cap schemas, arm auto_truncate, and replace kani's MCP bodies."""
    out: list = []
    for tool in tools:
        desc = getattr(tool, "desc", None)
        if isinstance(desc, str) and len(desc) > MCP_DESC_CAP:
            tool.desc = desc[:MCP_DESC_CAP] + "...(truncated)"
        grp, name = _extract_session(tool)
        schema = getattr(tool, "json_schema", None)
        if grp is None or not name or not schema:
            # Unrecognised shape: keep kani's body, just arm the truncation.
            if getattr(tool, "auto_truncate", None) is None:
                tool.auto_truncate = MCP_RESULT_CAP
            out.append(tool)
            continue
        from kani.ai_function import AIFunction

        out.append(
            AIFunction(
                _mcp_tool_body(grp, name),
                name=tool.name,
                desc=tool.desc,
                json_schema=schema,
                auto_truncate=MCP_RESULT_CAP,
            )
        )
    return out


class _TrackedClient:
    """Wraps an SDK client ACM just to remember it until it is closed."""

    def __init__(self, acm) -> None:
        self._acm = acm

    async def __aenter__(self):
        # Track BEFORE entering: a cancel landing during transport enter
        # leaves a suspended generator no drain could otherwise find.
        # Removal is idempotent, so double-listing is harmless.
        _PENDING_CLIENTS.append(self)
        try:
            return await self._acm.__aenter__()
        except BaseException:
            # Enter failed: drop the listing (the generator may still need a
            # drain — the caller's timeout path handles that).
            with contextlib.suppress(ValueError):
                _PENDING_CLIENTS.remove(self)
            raise

    async def __aexit__(self, *exc_info):
        try:
            return await self._acm.__aexit__(*exc_info)
        finally:
            with contextlib.suppress(ValueError):
                _PENDING_CLIENTS.remove(self)


def _track_client(factory):
    def tracked(*args, **kwargs):
        return _TrackedClient(factory(*args, **kwargs))

    return tracked


import mcp as _mcp_pkg  # noqa: E402  (after our local imports: patch last)
import mcp.client.session_group as _mcp_sg  # noqa: E402

_mcp_pkg.stdio_client = _track_client(_mcp_pkg.stdio_client)
_mcp_sg.sse_client = _track_client(_mcp_sg.sse_client)
_mcp_sg.streamable_http_client = _track_client(_mcp_sg.streamable_http_client)


async def _drain_pending_clients() -> None:
    """Close abandoned client generators IN THIS TASK (bounded).

    aclose() at the client's yield makes its anyio scopes unwind in the
    task that entered them, which is legal; letting GC do it later runs in
    an arbitrary task, cancels foreign scopes and murders the portal."""
    while _PENDING_CLIENTS:
        tracked = _PENDING_CLIENTS.pop()
        gen = getattr(tracked._acm, "gen", None)
        if gen is None:
            continue
        with contextlib.suppress(BaseException):
            async with asyncio.timeout(5.0):
                await gen.aclose()


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
    url = str(server.url).rstrip() if server.url else None
    if not url:
        raise MCPError("config", "remote server needs a url")
    headers = dict(server.headers) or None
    # Do NOT normalize trailing slashes: some servers (Django) 301-redirect
    # slashless to slash form, while others (Exa, Parallel on Cloudflare)
    # serve ONLY the exact registered path and 404 the slash variant. The
    # slash-URL retry in the 405/redirect fallback chain covers the Django
    # case per-failure instead of breaking the exact-path case up front.
    if server.transport == "sse":
        # SSE param timeouts are plain seconds.
        kwargs: dict = {"url": url, "headers": headers}
        if server.timeout is not None:
            kwargs["timeout"] = float(server.timeout)
        if server.sse_read_timeout is not None:
            kwargs["sse_read_timeout"] = float(server.sse_read_timeout)
        return SseServerParameters(**kwargs)
    # Streamable-HTTP param timeouts are timedeltas. Omitted (None) means the
    # SDK default (30s ops / 300s SSE read): passing an EXPLICIT timeout
    # changes the wire behavior — httpx.Timeout(30.0) collapses read/connect/
    # write/pool ALL to 30s, while the default keeps read at 300s. On
    # load-balanced edges (Cloudflare 32600 "Session terminated") the 30s
    # read races session setup, while the default leaves setup room and our
    # outer CONNECT_TIMEOUT still bounds the attempt. Only forward
    # user-configured values.
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
    if "405" in text or "method not allowed" in lowered:
        hint = "Try the other transport, or check the URL path"
        if "not followed" in lowered or "redirect" in lowered:
            hint = "Try the URL with a trailing slash"
        return MCPError("protocol", f"Server refused the transport (405). {hint}.")
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
        # Pass through the server's env/cwd: without them a server that fails
        # for env/cwd reasons reports a misleading stderr tail.
        import os as _os

        env = dict(_os.environ)
        env.update(server.env or {})
        proc = await asyncio.to_thread(
            subprocess.run,
            [cmd, *server.args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DIAGNOSE_TIMEOUT,
            env=env,
            cwd=server.cwd or None,
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
                try:
                    while not self._stop.is_set() and not self._reconnect.is_set():
                        # thread-safe wait; never blocks the owner loop. A plain
                        # asyncio.sleep checkpoint was tried (M6a) — the owner's
                        # foreign-cancel absorption below depends on parking
                        # OUTSIDE anyio's checkpoint machinery.
                        await asyncio.to_thread(self._reconnect.wait, 0.5)
                except asyncio.CancelledError as exc:
                    # A foreign cancel tagged "cancel scope" is an abandoned
                    # SDK generator's cross-task finalization lashing out;
                    # swallow it and keep the owner alive. Real teardown
                    # (untagged cancel, or _stop already set) must propagate.
                    if self._stop.is_set() or "cancel scope" not in str(exc):
                        raise
                    LOG.warning("mcp owner absorbed a foreign cancel; continuing")
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    # Belt: log it, surface it, keep the owner looping —
                    # an owner death would strand MCP for the whole session.
                    name = str(exc) or type(exc).__name__
                    if "shutdown" in name or "closed" in name:
                        # Interpreter/loop teardown (also: boot-test
                        # controllers that are never quit): exit quietly.
                        # Logging here hits a stderr lock held by daemon
                        # threads and fatally crashes the process.
                        return
                    LOG.warning("mcp owner wait failed: %s", name)
                    self._status(f"MCP error: {name[:150]}")
                    await asyncio.sleep(1.0)
        finally:
            # Cancellation (portal teardown) must still unwind the entered
            # contexts: they were entered MANUALLY, so they sit on no stack
            # frame and task cancellation would otherwise abandon the session
            # groups and any stdio child processes.
            try:
                await self._close_owned()
                await _drain_pending_clients()
            except Exception as exc:
                LOG.warning("mcp owner cleanup failed: %s", exc)

    async def _connect(self) -> None:
        """Connect each enabled server on its own.

        (Historically leaked clients from cancelled connects are drained
        first: _close_owned has already torn down healthy sessions, so
        anything still pending is a leak.)

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
        await _drain_pending_clients()
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
                # asyncio.timeout, NOT wait_for: wait_for runs the await in
                # an INNER task, so a timeout abandons the SDK's half-entered
                # anyio scopes in a task that can never legally exit them.
                # The later cross-task finalization corrupted cancel-scope
                # state and the owner itself died with "Attempted to exit a
                # cancel scope that isn't the current tasks's current cancel
                # scope", taking the whole agent portal (chat, catalog,
                # everything) with it. Same-task cancel unwinds the SDK
                # inside THIS task, which anyio allows.
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    tools = await context.__aenter__()
            except TimeoutError:
                # Close the abandoned SDK client HERE, in THIS task (see
                # _drain_pending_clients): GC finalization of it runs in
                # another task, cancels foreign cancel scopes and takes the
                # whole agent portal down with it.
                await _drain_pending_clients()
                LOG.warning(
                    "mcp %s timed out after %ss; other servers still load",
                    server.name,
                    CONNECT_TIMEOUT,
                )
                failed.append(f"{server.name}: timed out (check your connection)")
                continue
            except asyncio.CancelledError as exc:
                # The SDK's own anyio scopes can still unwind as a RAW
                # CancelledError on fast failures (DNS typo, refused port):
                # it never converts to TimeoutError, escaped every handler,
                # and used to kill the owner task, taking
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
                # "Session terminated" (32600): load-balanced edges (Exa is
                # behind Cloudflare) hand a later POST to a backend that
                # never saw the initialize, so the server kills the session.
                # A plain retry usually lands warm — retry once before
                # reporting the server dead.
                if "session terminated" in str(exc).lower():
                    LOG.info("mcp %s: session terminated; retrying once", server.name)
                    await _drain_pending_clients()
                    try:
                        retry_ctx = tools_from_mcp_servers(
                            [params],
                            blocked_tools=blocked or None,
                            component_name_hook=lambda name, _info, label=server.name: (
                                f"{label}.{name}"
                            ),
                        )
                        async with asyncio.timeout(CONNECT_TIMEOUT):
                            tools = await retry_ctx.__aenter__()
                    except Exception as retry_exc:
                        await _drain_pending_clients()
                        LOG.warning("mcp %s retry failed: %s", server.name, str(retry_exc)[:140])
                        failed.append(
                            f"{server.name}: {classify_exception(retry_exc).kind} "
                            f"({str(retry_exc)[:100]})"
                        )
                        continue
                    LOG.info("mcp %s connected on session retry", server.name)
                    contexts.append(retry_ctx)
                    all_tools.extend(tools)
                    all_names.extend(str(getattr(t, "name", "?")) for t in tools)
                    continue
                # Classify, don't dump: the status snack is the user's only
                # clue, so it must name the FIX (URL, headers, protocol), not
                # just the exception (the kinds come from classify_exception,
                # which unwraps TaskGroup wrappers to the real cause).
                error = classify_exception(exc)
                # Adaptive fallback chain for streamable_http refusals (405):
                # 1. Same URL as SSE (older transport, same endpoint).
                # 2. Slash-toggled URL as streamable (Django 301: the SDK
                #    only follows same-origin 307/308 for POST, so a slash
                #    redirect surfaces as "not followed" instead of working).
                # Same headers, same timeout, same task throughout.
                if server.transport == "streamable_http" and (
                    "405" in str(exc)
                    or "method not allowed" in str(exc).lower()
                    or "not followed" in str(exc).lower()
                    or "redirect" in str(exc).lower()
                ):
                    fallback_params: object | None = None
                    fallback_label = ""
                    sse_params = build_server_params(
                        server.model_copy(update={"transport": "sse"}),
                        is_mobile=self.is_mobile,
                    )
                    # Slash-toggled retry: build_server_params already
                    # normalizes toward slash form, so strip-then-add covers
                    # the (already-slashed) case too.
                    alt_url = str(server.url).rstrip() + "/"
                    slash_params = build_server_params(
                        server.model_copy(
                            update={
                                "url": alt_url,
                                "transport": "streamable_http",
                            }
                        ),
                        is_mobile=self.is_mobile,
                    )
                    for label, params in (
                        ("SSE", sse_params),
                        ("slash-URL", slash_params),
                    ):
                        LOG.info("mcp %s: streamable refused; retrying as %s", server.name, label)
                        await _drain_pending_clients()
                        try:
                            candidate = tools_from_mcp_servers(
                                [params],
                                blocked_tools=blocked or None,
                                component_name_hook=lambda name, _info, label=server.name: (
                                    f"{label}.{name}"
                                ),
                            )
                            async with asyncio.timeout(CONNECT_TIMEOUT):
                                tools = await candidate.__aenter__()
                        except Exception as fallback_exc:
                            await _drain_pending_clients()
                            LOG.warning(
                                "mcp %s %s fallback failed: %s",
                                server.name,
                                label,
                                str(fallback_exc)[:140],
                            )
                            continue
                        fallback_params = candidate
                        fallback_label = label
                        break
                    if fallback_params is None:
                        failed.append(f"{server.name}: {error.kind} ({str(exc)[:100]})")
                        continue
                    LOG.info("mcp %s connected via %s fallback", server.name, fallback_label)
                    contexts.append(fallback_params)
                    all_tools.extend(tools)
                    all_names.extend(str(getattr(t, "name", "?")) for t in tools)
                    continue
                detail = await _stdio_diagnose(server, error)
                LOG.warning("mcp %s failed (%s): %s", server.name, error.kind, detail)
                failed.append(f"{server.name}: {detail[:140]}")
                continue
            contexts.append(context)
            all_tools.extend(tools)
            all_names.extend(str(getattr(t, "name", "?")) for t in tools)

        # Untrusted-remote hygiene + kani's MCP wrapper crash fix:
        #  * cap what one can push into EVERY future request (a huge tool
        #    description is both a token bomb and a prompt-injection surface),
        #  * arm kani's paragraph-aware auto_truncate (its default for MCP
        #    tools is None, so 50-100KB of search/scrape results persisted
        #    verbatim and blew up prefill on every subsequent round), and
        #  * REBUILD each tool body: kani's wrapper raises ValueError on
        #    EmbeddedResource blocks (GitHub get_file_contents returns the file
        #    as a text resource), which makes the tool error out instead of
        #    returning the content.
        all_tools = _harden_mcp_tools(all_tools)
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

    async def test_on_owner(self, server: MCPServerConfig) -> list[dict]:
        """Test entry point for the MCP OWNER task (not the agent portal).

        hub.test() runs its own SDK task group: executed on the agent portal
        it contends with the live turn's group (cross-task cancel-scope
        fallout), and the Test button empirically unblocked hung turns — the
        probe perturbed whatever the turn was stuck on. The owner loop is the
        task that holds every other MCP context, so probes belong there.
        """
        return await self.test(server)

    async def test(self, server: MCPServerConfig) -> list[dict]:
        """Short-lived connect + list_tools with schema validation for the Settings UI."""
        try:
            params = build_server_params(server, is_mobile=self.is_mobile)
        except MCPError as exc:
            raise exc
        # Bound like _connect: an unresponsive server used to wedge the
        # Settings Test button for up to the SDK's 300s SSE default.
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                return await self._test_inner(server, params)
        except TimeoutError as exc:
            # Drain the half-entered SDK stack in THIS task before anything
            # else: without it the abandoned transport generator is finalized
            # by arbitrary-task GC and bricks foreign cancel scopes.
            await _drain_pending_clients()
            error = MCPError("unreachable", f"No response within {CONNECT_TIMEOUT:.0f}s.")
            raise MCPError(error.kind, await _stdio_diagnose(server, error)) from exc

    async def _test_inner(self, server: MCPServerConfig, params: object) -> list[dict]:
        try:
            context = tools_from_mcp_servers([params])  # type: ignore[list-item]
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
