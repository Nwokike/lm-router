"""Web search tool: MCP-SDK primary + keyless fallbacks.

The hosted provider IS an MCP server, so the app talks to it with the official
`mcp` client. These tests never touch the network: the primary is exercised
through stubs, and the fallbacks through an httpx MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from core import constants
from services.http import HttpService
from services.search import (
    FALLBACKS,
    SearchRateLimited,
    _is_rate_limited,
    _pick_search_tool,
    _render_text,
    build_search_tool,
    run_search_with_fallback,
)


def test_picks_the_search_tool_by_prefix() -> None:
    # Upstream may rename/add tools; we only need the search one.
    assert _pick_search_tool(["web_fetch_exa", "web_search_exa"]) == "web_search_exa"
    assert _pick_search_tool(["search"]) == "search"
    assert _pick_search_tool(["web_fetch_exa"]) is None
    assert _pick_search_tool([]) is None


def test_render_text_flattens_blocks() -> None:
    class _Block:
        def __init__(self, text):
            self.text = text

    assert _render_text("plain") == "plain"
    assert _render_text([_Block("one"), _Block("two")]) == "one\n\ntwo"
    assert _render_text([{"text": "dict block"}]) == "dict block"
    assert _render_text([]) == "No results."
    # Bounded so a huge page cannot flood the context window.
    assert len(_render_text("x" * 20000)) <= 8000


def test_rate_limit_is_recognised_from_the_exception() -> None:
    assert _is_rate_limited(SearchRateLimited("x")) is True
    assert _is_rate_limited(RuntimeError("HTTP 429 Too Many Requests")) is True
    assert _is_rate_limited(RuntimeError("rate limit exceeded")) is True
    assert _is_rate_limited(RuntimeError("connection reset")) is False


@pytest.mark.anyio
async def test_fallback_answers_when_primary_is_rate_limited(monkeypatch) -> None:
    """A capped hosted provider must not take the whole tool down."""

    async def _rate_limited(*_a, **_k):
        raise SearchRateLimited("free tier exhausted")

    monkeypatch.setattr("services.search.run_search", _rate_limited)

    def handler(request: httpx.Request) -> httpx.Response:
        if "wikipedia" in str(request.url):
            return httpx.Response(
                200,
                json={"query": {"search": [{"title": "Python", "snippet": "A language."}]}},
            )
        return httpx.Response(200, json={"AbstractText": "", "RelatedTopics": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await run_search_with_fallback(HttpService(client=client), query="python", top_k=3)
    assert "Wikipedia results:" in result
    assert "Python" in result


@pytest.mark.anyio
async def test_fallback_reports_honestly_when_every_source_fails(monkeypatch) -> None:
    async def _rate_limited(*_a, **_k):
        raise SearchRateLimited("free tier exhausted")

    monkeypatch.setattr("services.search.run_search", _rate_limited)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await run_search_with_fallback(HttpService(client=client), query="x", top_k=2)
    assert "unavailable" in result.lower()
    assert "rate limited" in result.lower()


def test_fallbacks_use_the_shared_http_client() -> None:
    """No fallback may need a package we do not already ship."""
    assert len(FALLBACKS) >= 2
    for source in FALLBACKS:
        assert callable(source)
        # They take the shared HttpService, never a bespoke client or SDK.
        assert "http" in source.__code__.co_varnames[: source.__code__.co_argcount]


def test_tool_is_built_without_side_effects() -> None:
    """Building the kani tool must not open a connection."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    tool = build_search_tool(HttpService(client=client))
    assert tool.name == "web_search"
    assert tool.json_schema["type"] == "object"


def test_endpoint_is_the_mcp_server() -> None:
    # The provider is an MCP server; the SDK negotiates the protocol.
    assert "/mcp" in constants.SEARCH_ENDPOINT
