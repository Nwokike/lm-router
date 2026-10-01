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

import asyncio
import html
from typing import Annotated

import httpx
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

# Bound like the MCP hub's CONNECT_TIMEOUT: a hung hosted endpoint used to
# hang the tool (and the turn) indefinitely — the hub bounds every connect.
SEARCH_PRIMARY_TIMEOUT = 20.0

# The hosted server exposes these; substring match survives vendor prefixes
# (exa_web_search, tavily_search) that a startswith check misses.
_SEARCH_TOOL_SUBSTRINGS = ("web_search", "search")


class SearchRateLimited(RuntimeError):
    """The upstream search provider refused us (HTTP 429)."""


def _pick_search_tool(tool_names: list[str]) -> str | None:
    lowered = [(name, name.lower()) for name in tool_names]
    for name, low in lowered:
        if "web_search" in low:
            return name
    for name, low in lowered:
        if "search" in low:
            return name
    return None


def _clamp_top_k(top_k: object, default: int = 5) -> int:
    try:
        return max(1, min(int(top_k), 10))  # type: ignore[call-overload]
    except TypeError, ValueError:
        return default


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
    manages its own transport. Provider-side failures RAISE (not return):
    the fallback loop in run_search_with_fallback must engage instead of
    stopping at a dead primary.
    """
    if not str(query or "").strip():
        raise ValueError("empty search query")
    try:
        async with asyncio.timeout(SEARCH_PRIMARY_TIMEOUT):
            async with streamable_http_client(constants.SEARCH_ENDPOINT) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    tool = _pick_search_tool([t.name for t in listed.tools])
                    if tool is None:
                        raise RuntimeError(
                            "provider exposes no search tool "
                            f"(saw {[t.name for t in listed.tools]})"
                        )
                    result = await session.call_tool(
                        tool,
                        {"query": query, "numResults": _clamp_top_k(top_k)},
                    )
                    if getattr(result, "isError", False):
                        detail = _render_text(result.content)
                        raise RuntimeError(f"provider tool error: {detail[:400]}")
                    text = _render_text(result.content)
                    if not text or text == "No results.":
                        return f"No results for '{query}' from the search provider."
                    return text
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
            "srlimit": _clamp_top_k(top_k),
            "format": "json",
        },
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(constants.SEARCH_TIMEOUT, connect=5.0),
    )
    if resp.status_code == 429:
        raise SearchRateLimited("Wikipedia rate limited the request.")
    resp.raise_for_status()
    hits = (resp.json().get("query") or {}).get("search") or []
    if not hits:
        return f"No results for '{query}' on Wikipedia."
    lines = ["Wikipedia results:"]
    for hit in hits:
        title = str(hit.get("title") or "")
        snippet = str(hit.get("snippet") or "")
        snippet = snippet.replace('<span class="searchmatch">', "").replace("</span>", "")
        snippet = html.unescape(snippet)
        page = str(hit.get("pageid") or "")
        url = f" (https://en.wikipedia.org/?curid={page})" if page else ""
        lines.append(f"- {title}: {snippet}{url}")
    return "\n".join(lines)


async def _duckduckgo_fallback(http: HttpService, query: str, top_k: int) -> str:
    """DuckDuckGo's keyless Instant Answer API."""
    limit = _clamp_top_k(top_k)
    resp = await http.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(constants.SEARCH_TIMEOUT, connect=5.0),
    )
    if resp.status_code == 429:
        raise SearchRateLimited("DuckDuckGo rate limited the request.")
    resp.raise_for_status()
    payload = resp.json()
    parts: list[str] = []
    abstract = str(payload.get("AbstractText") or "").strip()
    abstract_url = str(payload.get("AbstractURL") or "").strip()
    if abstract:
        parts.append(abstract + (f" ({abstract_url})" if abstract_url else ""))
    for topic in payload.get("RelatedTopics") or []:
        text = str(topic.get("Text") or "").strip()
        first_url = str(topic.get("FirstURL") or "").strip()
        if text and len(parts) < limit + 1:
            parts.append(f"- {text}" + (f" ({first_url})" if first_url else ""))
    if not parts:
        return f"No results for '{query}' on DuckDuckGo."
    return "DuckDuckGo results:\n" + "\n".join(parts)


FALLBACKS = (_wikipedia_fallback, _duckduckgo_fallback)


async def run_search_with_fallback(http: HttpService, query: str, top_k: int = 5) -> str:
    """Primary hosted search, then keyless sources when it is unavailable."""
    primary_limited = False
    try:
        return await run_search(http, query, top_k)
    except SearchRateLimited:
        primary_limited = True
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
    if primary_limited:
        # The provider itself said the free tier is exhausted — the only
        # case where "rate limited" is the truth.
        return (
            "Web search is rate limited right now (the free search provider "
            "is exhausted). Answer from your own knowledge and say the search "
            "was unavailable."
        )
    detail = errors[0] if errors else "no reachable search backend"
    # The old copy blamed rate limiting for EVERY failure while collecting
    # `errors` and never reading them — a DNS failure told the model a lie.
    return (
        f"Web search is unavailable right now ({detail}). Answer from your "
        "own knowledge and say the search was unavailable."
    )


def build_search_tool(http: HttpService) -> AIFunction:
    """kani tool: model calls web_search(query, top_k) during a turn."""

    async def web_search(
        query: Annotated[str, AIParam("Web search query to find sources and highlights")],
        top_k: Annotated[int, AIParam("Maximum number of results to retrieve (1-10)")] = 5,
    ) -> str:
        """Search the web and return sources with highlights. Use for current
        events, facts you are unsure about, or anything after your training
        cutoff. ALWAYS search first when you are not 100% sure about a tool,
        product, or setup step: research rather than guess, and never assume
        a similar-sounding name is the thing the user meant."""
        # Fail-soft by design: every backend failure returns an instructive
        # string ("answer from your own knowledge and say search was
        # unavailable") instead of raising, so one dead backend never costs
        # the turn an error card. is_tool_call_error never fires here.
        return await run_search_with_fallback(http, query, _clamp_top_k(top_k))

    return AIFunction(
        web_search,
        name="web_search",
        auto_truncate=MAX_RESULT_CHARS,
        auto_retry=False,
    )
