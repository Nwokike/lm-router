"""Web search: keyless hosted search over plain HTTP (no MCP SDK needed).

The endpoint is the same one OpenCode's own websearch tool calls. It needs a
browser-like User-Agent: python-urllib's default signature is banned there
(live probe), httpx with a Chrome UA returns200 with real results.
"""

import json

from kani.ai_function import AIFunction

from core import constants
from core.logging import LOG
from services.http import HttpService

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

MAX_RESULT_CHARS = 8000


def _render_results(payload: object) -> str:
    """Turn the MCP tools/call result into a compact text block for the model."""
    if not isinstance(payload, dict):
        return str(payload)[:MAX_RESULT_CHARS]
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return str(result)[:MAX_RESULT_CHARS]
    if result.get("isError"):
        detail = ""
        contents = result.get("content") or []
        if contents and isinstance(contents[0], dict):
            detail = str(contents[0].get("text", ""))
        return f"Search failed: {detail[:400]}" if detail else "Search failed."
    blocks = result.get("content") or []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "\n\n".join(parts).strip()
    return (text or "No results.")[:MAX_RESULT_CHARS]


async def run_search(http: HttpService, query: str, top_k: int = 5) -> str:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": UA,
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "lm-router", "version": "1"},
        },
    }
    session = await http.post(
        constants.SEARCH_ENDPOINT, headers=headers, json=init, timeout=constants.SEARCH_TIMEOUT
    )
    session.raise_for_status()
    headers = dict(headers)
    session_id = session.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": constants.SEARCH_TOOL_NAME,
            "arguments": {"query": query, "numResults": max(1, min(int(top_k), 10))},
        },
    }
    response = await http.post(
        constants.SEARCH_ENDPOINT, headers=headers, json=call, timeout=constants.SEARCH_TIMEOUT
    )
    response.raise_for_status()
    body = response.text
    # Streamable endpoints may answer as SSE: take the first data: line.
    if "data:" in body:
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
    try:
        payload = json.loads(body)
    except ValueError:
        return "Search returned an unreadable response."
    if isinstance(payload, dict) and payload.get("error"):
        return f"Search failed: {payload['error']}"
    return _render_results(payload)


def build_search_tool(http: HttpService) -> AIFunction:
    """kani tool: model calls web_search(query, top_k) during a turn."""

    async def web_search(query: str, top_k: int = 5) -> str:
        """Search the web and return sources with highlights. Use for current
        events, facts you are unsure about, or anything after your training."""
        try:
            return await run_search(http, query, top_k)
        except Exception as exc:
            LOG.warning("web search failed: %s", exc)
            return (
                "Search is unavailable right now. Answer from your own "
                "knowledge and say so if it matters."
            )

    # AIFunction wraps a plain callable: name and description come from the
    # function definition above (kani ai_function study).
    return AIFunction(web_search, "web_search")
