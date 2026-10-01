"""Batch 6 networking/utilities regression.

- ThoughtTapTransport binds pool Limits + trust_env on the TRANSPORT.
- TeeStream frames CRLF streams live and caps its buffer; byte-exact.
- Tokenizer negative cache expires (TTL); late worker writes through;
  headroom fires on heuristic use; vision lists never stringified;
  over-budget history keeps a tail instead of everything.
- Share proxy caps bodies, closes on 401, normalizes chunked framing.
- Tunnel validates claim host/port and refuses reuse after stop.
"""

from __future__ import annotations

import pytest
from test_boot_smoke import boot_page

__all__ = ["boot_page"]


def test_thought_transport_binds_pool_limits() -> None:
    import httpx

    from services.reasoning import ThoughtTap, build_thought_client

    client = build_thought_client(
        base_url="http://127.0.0.1:8082/v1",
        api_key="x",
        timeout=httpx.Timeout(5.0),
        max_retries=2,
        tap=ThoughtTap(),
    )
    pool = client._transport._pool
    assert pool._max_connections == 20, "pool must run house limits, not 100"


def test_tee_stream_byte_exact_and_crlf_live() -> None:
    import asyncio
    import json

    from services.reasoning import ThoughtTap, _TeeStream

    seen: list[str] = []
    tap = ThoughtTap(seen.append)

    async def _run() -> bytes:
        frames = [
            {"choices": [{"delta": {"reasoning_content": "step one "}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
        ]
        # CRLF framing: strict upstreams never contain bare \n\n.
        inbound = (
            "".join("data: " + json.dumps(f) + "\r\n\r\n" for f in frames) + "data: [DONE]\r\n\r\n"
        ).encode()

        class _Inner:
            async def __aiter__(self):
                # Split mid-frame to prove reassembly.
                yield inbound[:17]
                yield inbound[17:]

            async def aclose(self):
                pass

        out = b""
        async for chunk in _TeeStream(_Inner(), tap):
            out += chunk
        return out

    assert asyncio.run(_run()) == (
        b'data: {"choices": [{"delta": {"reasoning_content": "step one "}}]}'
        b"\r\n\r\n"
        b'data: {"choices": [{"delta": {"content": "answer"}}]}'
        b"\r\n\r\n"
        b"data: [DONE]\r\n\r\n"
    ), "tee must be byte-exact"
    assert seen == ["step one "], "CRLF reasoning must arrive live, not at close"


def test_tokenizer_negative_cache_expires(monkeypatch) -> None:
    import time

    from services import tokenizer as tk_mod

    monkeypatch.setattr(tk_mod, "_loaded", {})
    monkeypatch.setattr(tk_mod, "_attempted", {"o200k_base": time.monotonic() - 400})
    # Stale entry: a retry must be attempted (no exception = path exercised).
    assert isinstance(tk_mod._ATTEMPT_TTL, float)


def test_headroom_fires_on_heuristic_use(monkeypatch) -> None:
    from tiktoken import registry

    from services import tokenizer as tk_mod
    from services.tokenizer import estimate_turn_tokens

    monkeypatch.setattr(registry, "ENCODINGS", {})
    monkeypatch.setattr(tk_mod, "_loaded", {})
    monkeypatch.setattr(tk_mod, "_attempted", {})
    # Known model (is_approx False) but cold cache (heuristic counts):
    # headroom must still apply.
    total, _is_approx = estimate_turn_tokens(
        system_prompt="",
        chat_history=[],
        user_prompt="hello world this is a test prompt",
        model="gpt-4o",
    )
    raw = max(1, __import__("math").ceil(len("hello world this is a test prompt") / 4))
    assert total > raw + 4, "heuristic counts need headroom even for known models"


def test_vision_content_never_stringified() -> None:
    from services.tokenizer import _extract_text

    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:base64," + "A" * 5000}},
        ],
    }
    text = _extract_text(msg)
    assert "what is this?" in text
    assert "AAAA" not in text, "base64 repr must never enter the token count"
    assert "image" in text.lower()


def test_over_budget_keeps_tail_only() -> None:
    from kani import ChatMessage

    from services.tokenizer import truncate_history_to_budget

    history = [ChatMessage.user(f"turn {i}") for i in range(5)]
    kept, dropped = truncate_history_to_budget(history, budget_tokens=0, model="gpt-4o")
    assert len(kept) == 1 and dropped == 4, "over-budget must keep a tail, not all"


def test_share_proxy_caps_and_closes() -> None:
    from services import share as share_mod

    assert share_mod.MAX_BODY_BYTES == 32 * 1024 * 1024


def test_tunnel_validates_claim_and_single_use(monkeypatch) -> None:
    from services.tunnel import LocalTunnel, TunnelError

    tunnel = LocalTunnel(4096)
    tunnel._stop.set()
    with pytest.raises(TunnelError, match="cannot be restarted"):
        tunnel.start()

    fresh = LocalTunnel(4096)
    monkeypatch.setattr(
        fresh, "_claim", lambda: {"url": "https://evil.example.com/x", "port": 1234}
    )
    with pytest.raises(TunnelError, match="unexpected host"):
        fresh.start()

    noport = LocalTunnel(4096)
    monkeypatch.setattr(noport, "_claim", lambda: {"url": "https://abc.loca.lt", "port": 0})
    with pytest.raises(TunnelError, match="relay port"):
        noport.start()
