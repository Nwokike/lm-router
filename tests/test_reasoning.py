"""Reasoning capture: the model's thinking must reach the UI, not just a count.

kani flattens the upstream stream to answer text — `_stream_chat_completions`
yields `delta.content` and silently drops `reasoning_content` / `reasoning` /
`reasoning_details`. So the only place reasoning can be captured is the SSE
bytes themselves, which is what `ThoughtTapTransport` does. These tests pin
the wire-level behaviour and prove the pass-through is byte-exact.
"""

from __future__ import annotations

import json
import threading
import typing
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

import httpx
import pytest

from core.settings import AppSettings
from core.state import state
from services.agent import AgentService
from services.reasoning import (
    ThoughtTap,
    _reasoning_from_frame,
    _TeeStream,
    final_reasoning,
)

REASONING_FRAMES = [
    {"choices": [{"delta": {"reasoning_content": "Let me think. "}}]},
    {"choices": [{"delta": {"reasoning": "The sky scatters light. "}}]},
    {"choices": [{"delta": {"reasoning_details": [{"text": "step three"}]}}]},
]
ANSWER_FRAMES = [
    {"choices": [{"delta": {"content": "Because "}}]},
    {"choices": [{"delta": {"content": "of Rayleigh scattering."}}]},
]


def _sse(frames) -> bytes:
    return (
        "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
    ).encode()


# ── frame parsing ────────────────────────────────────────────────────────────


def test_reads_every_reasoning_dialect() -> None:
    assert _reasoning_from_frame(REASONING_FRAMES[0]) == "Let me think. "
    assert _reasoning_from_frame(REASONING_FRAMES[1]) == "The sky scatters light. "
    assert _reasoning_from_frame(REASONING_FRAMES[2]) == "step three"


def test_answer_frames_carry_no_reasoning() -> None:
    for frame in ANSWER_FRAMES:
        assert _reasoning_from_frame(frame) == ""


def test_responses_api_reasoning_event() -> None:
    frame = {"type": "response.reasoning_summary_text.delta", "delta": {"text": "hmm"}}
    assert _reasoning_from_frame(frame) == "hmm"


# ── the tee ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tee_captures_reasoning_and_passes_bytes_through_unchanged() -> None:
    """kani must still see the original stream; the tap is invisible to it."""
    seen: list[str] = []
    tap = ThoughtTap(seen.append)
    payload = _sse(REASONING_FRAMES + ANSWER_FRAMES)

    out = b""
    stream = _TeeStream(httpx.ByteStream(payload), tap)
    async for chunk in stream:
        out += chunk

    assert out == payload, "the tap altered the upstream bytes"
    assert "".join(seen) == "Let me think. The sky scatters light. step three"


@pytest.mark.anyio
async def test_tee_survives_a_malformed_frame() -> None:
    seen: list[str] = []
    tap = ThoughtTap(seen.append)
    payload = b"data: {not json\n\n" + _sse(REASONING_FRAMES[:1])
    out = b""
    async for chunk in _TeeStream(httpx.ByteStream(payload), tap):
        out += chunk
    assert out == payload
    assert seen == ["Let me think. "]


def test_callback_failure_is_logged_not_raised() -> None:
    calls: list[int] = []

    def boom(_: str) -> None:
        calls.append(1)
        raise RuntimeError("UI thread gone")

    tap = ThoughtTap(boom)
    tap.feed("still fine")  # must not propagate into the HTTP read loop
    assert calls == [1]


# ── non-streaming replies ────────────────────────────────────────────────────


def test_non_stream_reasoning_is_read_from_the_completion() -> None:
    class _Msg:
        reasoning = "The user wants exactly ok."

    class _Choice:
        message = _Msg()

    class _Completion:
        choices: typing.ClassVar[list] = [_Choice()]

    class _Message:
        text: typing.ClassVar[str] = "ok"
        extra: typing.ClassVar[dict] = {"openai_completion": _Completion()}

    assert final_reasoning(_Message()) == "The user wants exactly ok."
    # Content stays pure now that reasoning is a separate field.
    assert "[Reasoning:" not in _Message.text


def test_legacy_reasoning_blob_is_still_unwrapped() -> None:
    class _Message:
        text: typing.ClassVar[str] = "[Reasoning: I considered options]"

    assert final_reasoning(_Message()) == "I considered options"


def test_no_reasoning_returns_empty() -> None:
    class _Message:
        text: typing.ClassVar[str] = "plain answer"
        extra: typing.ClassVar[dict] = {}

    assert final_reasoning(_Message()) == ""


# ── the whole stack, over a real HTTP SSE server ──────────────────────────────


def test_reasoning_survives_a_real_kani_turn() -> None:
    """The regression that mattered: reasoning was silently empty in the app.

    Driven over real HTTP so the production ThoughtTapTransport is exercised —
    a MockTransport would REPLACE it and prove nothing.
    """
    payload = _sse(REASONING_FRAMES + ANSWER_FRAMES)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    old_base, old_running = state.gateway_base_url, state.gateway_running
    old_models = state.models
    state.gateway_base_url = f"http://127.0.0.1:{port}/v1"
    state.gateway_running = True
    state.models = [{"id": "m", "status": "active", "endpoint_type": "chat.completion"}]

    agent = AgentService(AppSettings())
    agent.start()
    try:
        thoughts: list[str] = []
        deltas: list[str] = []
        finished = threading.Event()
        agent.ensure_kani("m", "sys", thoughts.append)
        agent.start_turn(
            "why?",
            on_delta=deltas.append,
            on_done=lambda _m, _u: finished.set(),
            on_error=lambda _k, _t: finished.set(),
        )
        assert finished.wait(30), "turn never finished"
        assert "".join(thoughts) == "Let me think. The sky scatters light. step three"
        # The answer is unaffected by the tap.
        assert "".join(deltas) == "Because of Rayleigh scattering."
    finally:
        agent.stop()
        server.shutdown()
        server.server_close()
        state.gateway_base_url = old_base
        state.gateway_running = old_running
        state.models = old_models
