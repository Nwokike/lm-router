"""Web search through a hosted MCP server.

The search provider IS an MCP server, so it is spoken to with the official MCP
client (`mcp`, already present via `kani[mcp]`) rather than a hand-rolled
JSON-RPC-over-SSE implementation. That removes the `httpx-sse` dependency and
all protocol parsing: session negotiation, streaming and errors are the SDK's
job.

A hosted free tier rate limits, so a 429 is reported as such and the tool
degrades to keyless sources instead of surfacing a traceback.
"""

from __future__ import annotations

from typing import Annotated

from kani import AIParam
from kani.ai_function import AIFunction
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core import constants
from core.logging import LOG
from services.http import HttpService

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

MAX_RESULT_CHARS = 8000

# The hosted server exposes these; we pick the search one by prefix so a rename
# upstream does not silently disable the tool.
_SEARCH_TOOL_PREFIXES = ("web_search", "search")


class SearchRateLimited(RuntimeError):
    """The upstream search provider refused us (HTTP 429)."""


def _pick_search_tool(tool_names: list[str]) -> str | None:
    for name in tool_names:
        if name.lower().startswith(_SEARCH_TOOL_PREFIXES):
            return name
    return None


def _render_text(blocks: object) -> str:
    """Flatten an MCP tool result into text for the model."""
    if isinstance(blocks, str):
        return blocks[:MAX_RESULT_CHARS]
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
    text = "\n\n".join(parts).strip()
    return (text or "No results.")[:MAX_RESULT_CHARS]


def _is_rate_limited(exc: BaseException) -> bool:
    """Recognise a 429 without importing httpx just for the status code."""
    if isinstance(exc, SearchRateLimited):
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


async def run_search(http: HttpService, query: str, top_k: int = 5) -> str:
    """Call the hosted MCP search server with the official SDK client.

    `http` is kept for interface symmetry and fallback use; the MCP client
    manages its own transport.
    """
    try:
        async with streamable_http_client(constants.SEARCH_ENDPOINT) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool = _pick_search_tool([t.name for t in listed.tools])
                if tool is None:
                    return (
                        "Search is unavailable: the provider exposes no search "
                        f"tool (saw {[t.name for t in listed.tools]})."
                    )
                result = await session.call_tool(
                    tool,
                    {"query": query, "numResults": max(1, min(int(top_k), 10))},
                )
                if getattr(result, "isError", False):
                    detail = _render_text(result.content)
                    return f"Search failed: {detail[:400]}"
                return _render_text(result.content)
    except SearchRateLimited:
        raise
    except BaseException as exc:
        if _is_rate_limited(exc):
            raise SearchRateLimited(
                "The search provider's free tier is rate limited right now.",
            ) from exc
        raise


# ── Keyless fallback sources ────────────────────────────────────────────────
# The hosted MCP endpoint is one provider on one free tier, so a 429 used to
# take the whole tool down. These need no key and no extra package — just
# httpx, which Flet and the MCP SDK already bring.


async def _wikipedia_fallback(http: HttpService, query: str, top_k: int) -> str:
    """Wikipedia's public API: keyless, generous, good for factual lookups."""
    resp = await http.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, min(int(top_k), 10)),
            "format": "json",
        },
        headers={"User-Agent": UA},
        timeout=constants.SEARCH_TIMEOUT,
    )
    if resp.status_code == 429:
        raise SearchRateLimited("Wikipedia rate limited the request.")
    resp.raise_for_status()
    hits = (resp.json().get("query") or {}).get("search") or []
    if not hits:
        return ""
    lines = ["Wikipedia results:"]
    for hit in hits:
        title = str(hit.get("title") or "")
        snippet = str(hit.get("snippet") or "")
        snippet = snippet.replace('<span class="searchmatch">', "").replace("</span>", "")
        lines.append(f"- {title}: {snippet}")
    return "\n".join(lines)


async def _duckduckgo_fallback(http: HttpService, query: str, top_k: int) -> str:
    """DuckDuckGo's keyless Instant Answer API."""
    resp = await http.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        headers={"User-Agent": UA},
        timeout=constants.SEARCH_TIMEOUT,
    )
    if resp.status_code == 429:
        raise SearchRateLimited("DuckDuckGo rate limited the request.")
    resp.raise_for_status()
    payload = resp.json()
    parts: list[str] = []
    abstract = str(payload.get("AbstractText") or "").strip()
    if abstract:
        parts.append(abstract)
    for topic in payload.get("RelatedTopics") or []:
        text = str(topic.get("Text") or "").strip()
        if text and len(parts) < int(top_k) + 1:
            parts.append(f"- {text}")
    if not parts:
        return ""
    return "DuckDuckGo results:\n" + "\n".join(parts)


FALLBACKS = (_wikipedia_fallback, _duckduckgo_fallback)


async def run_search_with_fallback(http: HttpService, query: str, top_k: int = 5) -> str:
    """Primary hosted search, then keyless sources when it is unavailable."""
    try:
        return await run_search(http, query, top_k)
    except SearchRateLimited:
        LOG.warning("search provider rate limited; trying fallback sources")
    except Exception as exc:
        LOG.warning("search provider failed (%s); trying fallback sources", exc)

    errors: list[str] = []
    for source in FALLBACKS:
        try:
            result = await source(http, query, top_k)
        except Exception as exc:
            LOG.debug("fallback %s failed: %s", getattr(source, "__name__", source), exc)
            errors.append(str(exc)[:120])
            continue
        if result:
            return result
    return (
        "Web search is unavailable right now (the free search provider is rate "
        "limited). Answer from your own knowledge and say the search was "
        "unavailable."
    )


def build_search_tool(http: HttpService) -> AIFunction:
    """kani tool: model calls web_search(query, top_k) during a turn."""

    async def web_search(
        query: Annotated[str, AIParam("Web search query to find sources and highlights")],
        top_k: Annotated[int, AIParam("Maximum number of results to retrieve (1-10)")] = 5,
    ) -> str:
        """Search the web and return sources with highlights. Use for current
        events, facts you are unsure about, or anything after your training cutoff."""
        # Do NOT swallow failures into a friendly-sounding string: kani turns a
        # raised exception into a tool result with is_tool_call_error=True,
        # which renders as a visible error card in chat (audit D).
        return await run_search_with_fallback(http, query, top_k)

    return AIFunction(
        web_search,
        name="web_search",
        auto_truncate=MAX_RESULT_CHARS,
        auto_retry=False,
    )
