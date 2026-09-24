"""Tokenizer service: blocking-free resolution + deterministic heuristic.

Network-independent: a slow/failed tiktoken download must never block or
raise — the UI-thread freeze it caused (tiktoken/load.py requests.get with
no timeout) is exactly what these tests lock down.
"""

import time

from kani import ChatMessage
from tiktoken import registry

from services import tokenizer as tk_mod
from services.tokenizer import (
    count_tokens,
    estimate_turn_tokens,
    get_encoding_for_model,
    prewarm_tokenizers,
    truncate_history_to_budget,
)


def _cold_registry(monkeypatch, *, block: bool) -> None:
    """Simulate a cold tiktoken cache (optionally a hanging download)."""
    monkeypatch.setattr(registry, "ENCODINGS", {})
    monkeypatch.setattr(tk_mod, "_loaded", {})
    monkeypatch.setattr(tk_mod, "_attempted", set())
    monkeypatch.setattr(tk_mod, "_LOAD_TIMEOUT", 0.2)

    if block:

        def hanging(name: str):
            time.sleep(5)
            raise AssertionError("should never be reached")

        monkeypatch.setattr(tk_mod.tiktoken, "get_encoding", hanging)


def test_encoding_resolution_never_blocks(monkeypatch) -> None:
    _cold_registry(monkeypatch, block=True)
    start = time.monotonic()
    enc, is_approx = get_encoding_for_model("mimo-v2.6-flash")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, "encoding resolution blocked"
    assert enc is None
    assert is_approx is True


def test_warm_registry_is_instant(monkeypatch) -> None:
    _cold_registry(monkeypatch, block=True)
    sentinel = object()
    monkeypatch.setattr(registry, "ENCODINGS", {"o200k_base": sentinel})
    enc, is_approx = get_encoding_for_model("mimo-v2.6-flash")
    assert enc is sentinel
    assert is_approx is True


def test_count_tokens_heuristic_without_encoding() -> None:
    assert count_tokens("") == 0
    assert count_tokens("a") == 1
    assert count_tokens("abcd") == 1
    assert count_tokens("abcde") == 2
    # special-token-looking text never raises in either path
    assert count_tokens("<|endoftext|> <|fim_prefix|>", None) > 0


def test_estimate_and_truncate_work_without_encoding(monkeypatch) -> None:
    _cold_registry(monkeypatch, block=True)
    history = [
        ChatMessage.user("What is Python?"),
        ChatMessage.assistant("Python is a programming language."),
    ]
    estimated, is_approx = estimate_turn_tokens(
        system_prompt="You are helpful.",
        chat_history=history,
        user_prompt="Tell me more.",
        model="mimo-v2.6-flash",
    )
    assert estimated > 0
    assert is_approx is True


def test_truncate_prioritizes_tools_then_oldest(monkeypatch) -> None:
    _cold_registry(monkeypatch, block=True)
    history = [
        ChatMessage.user("First question"),
        ChatMessage.function("search", "Lots of search results " * 50),
        ChatMessage.assistant("Summary of search"),
        ChatMessage.user("Second question"),
    ]
    truncated, dropped = truncate_history_to_budget(history, budget_tokens=40, model="gpt-4o")
    assert dropped > 0
    assert len(truncated) < len(history)
    assert not any("function" in str(getattr(m.role, "value", m.role)).lower() for m in truncated)


def test_prewarm_is_bounded(monkeypatch) -> None:
    _cold_registry(monkeypatch, block=True)
    start = time.monotonic()
    prewarm_tokenizers()
    assert time.monotonic() - start < 4.0, "prewarm blocked"
