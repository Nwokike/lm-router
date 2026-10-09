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
# Negative cache with TTL: a transient failure (offline at boot, then online)
# used to disable real tokenization for the PROCESS lifetime. Entries older
# than this are retried.
_ATTEMPT_TTL = 300.0
_attempted: dict[str, float] = {}
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
    import time as _time

    enc = _tk_registry.ENCODINGS.get(name)
    if enc is not None:
        return enc
    with _attempt_lock:
        attempted_at = _attempted.get(name)
        if attempted_at is not None and _time.monotonic() - attempted_at < _ATTEMPT_TTL:
            return _tk_registry.ENCODINGS.get(name)
        _attempted[name] = _time.monotonic()
    result: list[tiktoken.Encoding | None] = [None]

    def _worker() -> None:
        try:
            got = tiktoken.get_encoding(name)
        except Exception as exc:
            LOG.debug("tiktoken load failed for %s: %s", name, exc)
            return
        result[0] = got
        # Write-through: a download finishing just after the join used to
        # evaporate (only the local `result` saw it) while the negative
        # cache blocked every retry. Publish late arrivals so the next
        # call picks them up.
        with _attempt_lock:
            _loaded[name] = got

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


# Rough per-image cost when a vision block carries no better signal. Real
# pricing is 85-1700 tokens/image by size/detail; this keeps one image from
# either vanishing (0) or exploding (base64 repr stringified = tens of
# thousands of phantom tokens).
IMAGE_TOKEN_ALLOWANCE = 1000


def _extract_text(message: Any) -> str:
    """Extract plain text from a ChatMessage, dict, or string."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if isinstance(content, list):
        # Vision-shaped content blocks: join text parts, allow a flat cost
        # per image part, never str() the block list (keys + base64 repr
        # would explode the count by tens of thousands of phantom tokens).
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif "image" in str(block.get("type") or ""):
                parts.append(f"[image ~{IMAGE_TOKEN_ALLOWANCE} tokens]")
        if parts:
            return "\n".join(parts)
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    text = getattr(message, "text", None)
    if text is not None:
        return str(text)
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


# Per-message token counts, keyed by (id(msg), len(extracted text)). Messages
# are immutable once appended to a kani history, so a send that re-walks an
# unchanged prefix costs ZERO tiktoken encodes instead of one per message.
# The id() can be recycled after a message is dropped, which only ever happens
# after a history mutation (trim/clear) — those paths clear the cache.
_HISTORY_TOKEN_CACHE: dict[tuple[int, int], int] = {}

_HISTORY_TOKEN_CACHE_MAX = 4096


def _cached_msg_tokens(msg: Any, encoding: Any) -> int:
    text = _extract_text(msg)
    # role joins the key: a recycled id (message dropped, new one allocated at
    # the same address) with the same text length but a different role would
    # otherwise return another message's count.
    role = str(getattr(msg, "role", "")).lower()
    key = (id(msg), len(text), len(role))
    hit = _HISTORY_TOKEN_CACHE.get(key)
    if hit is None:
        hit = count_tokens(text, encoding)
        if len(_HISTORY_TOKEN_CACHE) >= _HISTORY_TOKEN_CACHE_MAX:
            _HISTORY_TOKEN_CACHE.clear()
        _HISTORY_TOKEN_CACHE[key] = hit
    return hit


def _reset_history_token_cache() -> None:
    """Drop cached per-message counts (call after mutating a history)."""
    _HISTORY_TOKEN_CACHE.clear()


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
        total += _cached_msg_tokens(msg, encoding) + 4

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

    if encoding is None:
        # Heuristic counts undercount CJK/code (1-2 chars/token vs the ~4
        # assumed): add 20% headroom so heuristic budgets never promise more
        # context than the model actually has. Triggered on encoding-is-None
        # (heuristic actually used), NOT on is_approx (mapping unknown): a
        # cold known-model box counts heuristically with zero headroom
        # otherwise — exactly the case this protects.
        total = int(total * 1.2) + 1
    return total, is_approx


def history_tail(chat_history: list[Any], n: int) -> list[Any]:
    """Last n messages, preserving order (budget-overflow fallback)."""
    return list(chat_history[-n:]) if n > 0 else []


def truncate_history_to_budget(
    chat_history: list[Any],
    budget_tokens: int,
    model: str = "",
) -> tuple[list[Any], int]:
    """Trim oldest messages from history to fit within budget_tokens.

    Prioritizes dropping oldest tool results and assistant turns first.
    Returns (kept_history, dropped_count).
    """
    if not chat_history:
        return [], 0
    if budget_tokens <= 0:
        # The prompt alone exceeds the budget: returning the FULL history is
        # precisely inverted (maximum over-budget input gets zero trimming).
        # Keep only the latest turn so the request has a chance.
        return history_tail(chat_history, 1), len(chat_history) - 1

    encoding, _ = get_encoding_for_model(model)
    history = list(chat_history)
    dropped = 0

    # Per-message count cache: the old current_tokens() re-encoded the whole
    # history per eviction (O(n^2) on long sessions, synchronously in
    # preflight). Counts are stable — text doesn't change while trimming.
    counts = [count_tokens(_extract_text(m), encoding) + 4 for m in history]
    total = sum(counts)

    def _drop_at(idx: int) -> None:
        nonlocal total, dropped
        total -= counts.pop(idx)
        history.pop(idx)
        dropped += 1

    # First pass: drop oldest function/tool messages
    idx = 0
    while total > budget_tokens and idx < len(history) - 1:
        role = _extract_role(history[idx])
        if "function" in role or "tool" in role:
            _drop_at(idx)
        else:
            idx += 1

    # Second pass: drop oldest turns from the beginning
    while total > budget_tokens and len(history) > 1:
        _drop_at(0)

    return history, dropped


def prewarm_tokenizers() -> None:
    """Bounded warm-up so the first chat turn never waits on a download.

    Each encoding gets at most _LOAD_TIMEOUT; failures fall back to the
    heuristic and are retried lazily on later turns.
    """
    # o200k_harmony covers gpt-oss-* models (control tokens like
    # <|channel|> miscount as plain text under o200k_base otherwise).
    for name in (DEFAULT_FALLBACK_ENCODING, "cl100k_base", "o200k_harmony"):
        _load_encoding(name)
    LOG.info(
        "tokenizers pre-warmed (registry=%s)",
        sorted(_tk_registry.ENCODINGS) or "empty (heuristic fallback active)",
    )
