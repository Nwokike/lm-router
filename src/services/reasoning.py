"""Reasoning capture for kani turns.

kani flattens the upstream stream to answer text: `_stream_chat_completions`
reads `delta.content` and yields it, and any `reasoning_content` /
`reasoning` / `reasoning_details` delta on the same chunk is **discovered and
thrown away**. So there is nothing left to tap above the HTTP layer — the
capture has to happen where DDGS does it, on the SSE bytes themselves.

`ThoughtTapTransport` is an httpx transport (httpx is already a dependency,
and the OpenAI client accepts a custom transport) that tees
`text/event-stream` responses and pulls the reasoning fields out of each
`data:` frame. The original bytes are passed through untouched, so kani still
sees exactly the stream the upstream sent.

The tap is shared by reference with the engine so a new turn can swap its
callback without rebuilding the client.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from kani.engines.openai import OpenAIEngine

from core.logging import LOG

ThoughtCB = Callable[[str], None]

# Fields seen in the wild, across providers. The Kiri gateway is a byte-exact
# pass-through for streaming (it rewrites only the model id), so every dialect
# the upstream emits arrives intact.
_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_details")


def _flatten(value: Any) -> str:
    """reasoning_details is sometimes a list of structured parts."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                if item.get("text"):
                    out.append(str(item["text"]))
            else:
                text = getattr(item, "text", None)
                if text:
                    out.append(str(text))
        return "".join(out)
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value)


def _reasoning_from_frame(payload: dict) -> str:
    """Extract reasoning text from one decoded SSE frame."""
    # Responses-API typed events, e.g.
    # "response.reasoning_summary_text.delta" / "response.reasoning_text.done".
    kind = str(payload.get("type") or "")
    if "reasoning" in kind and not payload.get("choices"):
        delta = payload.get("delta") or {}
        text = delta.get("text") if isinstance(delta, dict) else None
        return _flatten(text if text is not None else payload.get("text"))

    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        for key in _REASONING_KEYS:
            value = delta.get(key)
            if value:
                return _flatten(value)
        # Non-stream style payload occasionally rides the stream.
        message = choice.get("message") or {}
        if isinstance(message, dict):
            for key in _REASONING_KEYS:
                value = message.get(key)
                if value:
                    return _flatten(value)
    return ""


class ThoughtTap:
    """Mutable holder so a new turn can retarget the callback in place."""

    def __init__(self, callback: ThoughtCB | None = None) -> None:
        self.callback = callback

    def feed(self, text: str) -> None:
        if not text or self.callback is None:
            return
        try:
            self.callback(text)
        except Exception as exc:
            # A UI hiccup must never kill the turn, but never swallow it
            # silently either: that is how a Thinking block "disappears".
            LOG.warning("reasoning callback failed: %s", exc)


class _TeeStream(httpx.AsyncByteStream):
    """Pass-through byte stream that scans SSE frames for reasoning."""

    def __init__(self, inner: httpx.AsyncByteStream, tap: ThoughtTap) -> None:
        self._inner = inner
        self._tap = tap
        self._buffer = b""

    async def __aiter__(self):
        async for chunk in self._inner:
            self._buffer += chunk
            # SSE frames are separated by a blank line.
            while b"\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split(b"\n\n", 1)
                self._scan(frame)
            yield chunk  # unchanged: kani must see the original bytes
        self._scan(self._buffer)
        self._buffer = b""

    def _scan(self, frame: bytes) -> None:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == b"[DONE]":
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if isinstance(payload, dict):
                self._tap.feed(_reasoning_from_frame(payload))

    async def aclose(self) -> None:
        await self._inner.aclose()


class ThoughtTapTransport(httpx.AsyncHTTPTransport):
    """httpx transport that tees SSE reasoning without altering the stream."""

    def __init__(self, tap: ThoughtTap, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tap = tap

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type.lower():
            response.stream = _TeeStream(response.stream, self._tap)
        return response


class ReasoningEngine(OpenAIEngine):
    """OpenAIEngine wired to a reasoning tap.

    Kept as a subclass so the engine still IS a kani engine (isinstance checks
    in kani keep working) and so `on_thought` can be retargeted per turn
    without rebuilding the HTTP client.
    """

    def __init__(self, *args: Any, tap: ThoughtTap | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tap = tap if tap is not None else ThoughtTap()

    @property
    def on_thought(self) -> ThoughtCB | None:
        return self.tap.callback

    @on_thought.setter
    def on_thought(self, value: ThoughtCB | None) -> None:
        self.tap.callback = value


def build_thought_client(
    *,
    base_url: str,
    api_key: str,
    timeout: httpx.Timeout,
    max_retries: int,
    tap: ThoughtTap,
) -> httpx.AsyncClient:
    """An httpx client whose streams are scanned for reasoning."""
    return httpx.AsyncClient(
        transport=ThoughtTapTransport(tap),
        timeout=timeout,
        headers={"User-Agent": "LM-Router"},
    )


def final_reasoning(message: Any) -> str:
    """Reasoning from a COMPLETED (non-streamed) reply.

    Since the gateway's non-stream fix, reasoning arrives as a structured
    `message.reasoning` on the OpenAI choice, and it is additive — content
    stays pure. Older builds folded it into a `[Reasoning: ...]` content blob,
    so that shape is still unwrapped as a fallback.
    """
    extra = getattr(message, "extra", None)
    completion = extra.get("openai_completion") if hasattr(extra, "get") else None
    choices = getattr(completion, "choices", None)
    if choices:
        try:
            msg = getattr(choices[0], "message", None)
        except IndexError, TypeError:
            msg = None
        for key in _REASONING_KEYS:
            value = getattr(msg, key, None)
            if value:
                return _flatten(value)
    text = str(getattr(message, "text", "") or "")
    marker = "[Reasoning:"
    if marker in text and text.rstrip().endswith("]"):
        try:
            return text.split(marker, 1)[1].rsplit("]", 1)[0].strip()
        except IndexError:
            return ""
    return ""


__all__ = [
    "ReasoningEngine",
    "ThoughtCB",
    "ThoughtTap",
    "ThoughtTapTransport",
    "build_thought_client",
    "final_reasoning",
]
