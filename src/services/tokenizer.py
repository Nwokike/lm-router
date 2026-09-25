"""Tokenizer and token budgeting service — NEVER blocks the UI thread.

tiktoken's first load of an encoding downloads the BPE ranks with
`requests.get()` and NO timeout (tiktoken/load.py). Running that inline on
the Flet event thread froze the entire app with no exception and no log when
the network was slow — chat now goes through a worker, and this module
never blocks either:

1. already in tiktoken's in-process registry (prewarmed / previously
   loaded) -> instant
2. otherwise load on a short-lived daemon thread with a hard join budget
3. on timeout/failure -> `None`, and counting falls back to a deterministic
   character heuristic so chat always works
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

import tiktoken
from tiktoken import registry as _tk_registry

from core.logging import LOG

DEFAULT_FALLBACK_ENCODING = "o200k_base"
_LOAD_TIMEOUT = 1.5
_attempted: set[str] = set()
_attempt_lock = threading.Lock()
_loaded: dict[str, tiktoken.Encoding] = {}


def _encoding_name(model_id: str) -> tuple[str, bool]:
    """(encoding name, is_approximate) — pure lookup, never downloads."""
    if model_id:
        try:
            return tiktoken.encoding_name_for_model(model_id), False
        except KeyError:
            pass
    return DEFAULT_FALLBACK_ENCODING, True


def _load_encoding(name: str) -> tiktoken.Encoding | None:
    """Resolve an encoding with a hard time budget; None when unavailable."""
    enc = _tk_registry.ENCODINGS.get(name)
    if enc is not None:
        return enc
    with _attempt_lock:
        if name in _attempted:
            return _tk_registry.ENCODINGS.get(name)
        _attempted.add(name)
    result: list[tiktoken.Encoding | None] = [None]

    def _worker() -> None:
        try:
            result[0] = tiktoken.get_encoding(name)
        except Exception as exc:
            LOG.debug("tiktoken load failed for %s: %s", name, exc)

    thread = threading.Thread(target=_worker, name=f"tiktoken-{name}", daemon=True)
    thread.start()
    thread.join(_LOAD_TIMEOUT)
    enc = result[0]
    if enc is not None:
        _loaded[name] = enc
    return enc


def get_encoding_for_model(model_id: str) -> tuple[tiktoken.Encoding | None, bool]:
    """Return (encoding | None, is_approximate). Blocking-free by design."""
    name, is_approx = _encoding_name(model_id)
    enc = _loaded.get(name) or _load_encoding(name)
    return enc, is_approx


class HeuristicTokenizer:
    """Estimate ~4 characters per token without touching the network.

    kani falls back to `tiktoken.get_encoding()` when given no tokenizer,
    which downloads BPE ranks with no timeout — a 40-60s stall on a cold
    machine, and a first-turn hang in CI where no cache exists yet. Handing
    kani this object instead blocks that path entirely: counts are slightly
    approximate until the real encoding lands in the registry, but a turn
    always starts immediately.
    """

    name = "heuristic"

    def encode(self, text: str) -> list[int]:
        return list(range(max(1, math.ceil(len(str(text)) / 4))))

    encode_ordinary = encode


def tokenizer_or_heuristic(model_id: str):
    """The real tiktoken Encoding when available within budget, else local."""
    encoding, _approx = get_encoding_for_model(model_id)
    if encoding is not None:
        return encoding
    return HeuristicTokenizer()


def count_tokens(text: str, encoding: tiktoken.Encoding | None = None) -> int:
    """Count tokens safely (encode_ordinary never raises on special-token text).

    No encoding -> deterministic heuristic (~4 chars/token) so callers never
    block on a network download.
    """
    if not text:
        return 0
    if encoding is None:
        return max(1, math.ceil(len(text) / 4))
    return len(encoding.encode_ordinary(text))


def _extract_text(message: Any) -> str:
    """Extract plain text from a ChatMessage, dict, or string."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return str(message.get("content") or "")
    text = getattr(message, "text", None)
    if text is not None:
        return str(text)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    return str(content or "")


def _extract_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "user")
    role = getattr(message, "role", None)
    if role is not None:
        val = getattr(role, "value", None)
        return str(val if val is not None else role).lower()
    return "user"


def estimate_turn_tokens(
    system_prompt: str,
    chat_history: list[Any],
    user_prompt: str,
    tools: list[Any] | None = None,
    model: str = "",
) -> tuple[int, bool]:
    """Estimate total prompt tokens for an upcoming turn.

    Returns (estimated_tokens, is_approximate).
    Accounts for OpenAI chat-format overhead (~4 tokens/msg + 3 priming tokens).
    """
    encoding, is_approx = get_encoding_for_model(model)
    total = 3  # priming tokens (<|im_start|>assistant)

    if system_prompt:
        total += count_tokens(system_prompt, encoding) + 4

    for msg in chat_history:
        text = _extract_text(msg)
        total += count_tokens(text, encoding) + 4

    if user_prompt:
        total += count_tokens(user_prompt, encoding) + 4

    if tools:
        for tool in tools:
            # Serialized tool name, description, schema
            schema = getattr(tool, "json_schema", None)
            if schema:
                try:
                    total += count_tokens(json.dumps(schema), encoding) + 6
                except TypeError, ValueError:
                    total += 20
            else:
                desc = getattr(tool, "desc", "") or ""
                name = getattr(tool, "name", "") or ""
                total += count_tokens(f"{name}: {desc}", encoding) + 6

    return total, is_approx


def truncate_history_to_budget(
    chat_history: list[Any],
    budget_tokens: int,
    model: str = "",
) -> tuple[list[Any], int]:
    """Trim oldest messages from history to fit within budget_tokens.

    Prioritizes dropping oldest tool results and assistant turns first.
    Returns (kept_history, dropped_count).
    """
    if not chat_history or budget_tokens <= 0:
        return list(chat_history), 0

    encoding, _ = get_encoding_for_model(model)
    history = list(chat_history)
    dropped = 0

    def current_tokens() -> int:
        return sum(count_tokens(_extract_text(m), encoding) + 4 for m in history)

    # First pass: drop oldest function/tool messages
    idx = 0
    while current_tokens() > budget_tokens and idx < len(history) - 1:
        role = _extract_role(history[idx])
        if "function" in role or "tool" in role:
            history.pop(idx)
            dropped += 1
        else:
            idx += 1

    # Second pass: drop oldest turns from the beginning
    while current_tokens() > budget_tokens and len(history) > 1:
        history.pop(0)
        dropped += 1

    return history, dropped


def prewarm_tokenizers() -> None:
    """Bounded warm-up so the first chat turn never waits on a download.

    Each encoding gets at most _LOAD_TIMEOUT; failures fall back to the
    heuristic and are retried lazily on later turns.
    """
    for name in (DEFAULT_FALLBACK_ENCODING, "cl100k_base"):
        _load_encoding(name)
    LOG.info(
        "tokenizers pre-warmed (registry=%s)",
        sorted(_tk_registry.ENCODINGS) or "empty (heuristic fallback active)",
    )
