"""MCP hub: user-registered servers become kani tools.

Enabled stdio/remote servers are connected ONCE and held open for the app
lifetime (spawning npx per chat turn would be unusable); config changes
re-apply the whole set through the agent portal so connections stay on one
event loop (kani/mcp studies). Errors are classified for friendly UI copy.
"""

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


class MCPError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # unreachable | protocol | auth | tool | config


def build_server_params(server: MCPServerConfig) -> object:
    if server.transport == "stdio":
        if not server.command:
            raise MCPError("config", "stdio server needs a command")
        env = dict(server.headers) if server.headers else None
        return StdioServerParameters(command=server.command, args=list(server.args), env=env)
    url = str(server.url) if server.url else None
    if not url:
        raise MCPError("config", "remote server needs a url")
    if server.transport == "sse":
        return SseServerParameters(url=url, headers=dict(server.headers) or None)
    return StreamableHttpParameters(url=url, headers=dict(server.headers) or None)


def enabled_params(settings: AppSettings) -> list[object]:
    params = []
    for server in settings.mcp_servers:
        if server.enabled:
            params.append(build_server_params(server))
    return params


def classify_exception(exc: Exception) -> MCPError:
    text = str(exc)
    if isinstance(exc, MCPError):
        return exc
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return MCPError("unreachable", "Server unreachable. Check the URL or command.")
    lowered = text.lower()
    if "unauthorized" in lowered or "401" in text or "403" in text:
        return MCPError("auth", "Authentication failed. Check the headers or key.")
    if "protocol" in lowered or "-32601" in text or "-32602" in text:
        return MCPError("protocol", "Server speaks an incompatible protocol version.")
    return MCPError("tool", text[:400])


@dataclass
class MCPHub:
    settings: AppSettings
    tools: list = field(default_factory=list)  # AIFunction list for kani
    names: list[str] = field(default_factory=list)
    generation: int = 0
    _context: object | None = None  # entered async CM (kani tools_from_mcp_servers)

    async def apply(self, params: list[object]) -> None:
        """(Re)connect the enabled set. Runs on the agent portal loop."""
        await self.close()
        if not params:
            self.generation += 1
            return
        context = tools_from_mcp_servers(
            params,
            component_name_hook=lambda name, info: f"{info.name}.{name}",
        )
        tools = await context.__aenter__()
        self._context = context
        self.tools = list(tools)
        self.names = [str(getattr(t, "name", "?")) for t in self.tools]
        self.generation += 1
        LOG.info("mcp connected: %d tools from %d servers", len(self.tools), len(params))

    async def close(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                LOG.warning("mcp disconnect: %s", exc)
        self.tools = []
        self.names = []
        self.generation += 1

    async def test(self, server: MCPServerConfig) -> list[str]:
        """Short-lived connect + list_tools for the Settings UI."""
        try:
            params = build_server_params(server)
        except MCPError as exc:
            raise exc
        try:
            context = tools_from_mcp_servers([params])
            tools = await context.__aenter__()
            try:
                return [str(getattr(t, "name", "?")) for t in tools]
            finally:
                await context.__aexit__(None, None, None)
        except Exception as exc:
            raise classify_exception(exc) from exc
