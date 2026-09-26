"""Model test bench: probe models through the LOCAL gateway.

Port of the router's `tools/test-all-models.mjs` verdicts (OK / RATE / FAIL /
EMPTY, exactly one 429 retry after 4s) and of the local console's Test
button. Zero auth, the user's own IP. Sweeps are strictly serial — the
reference tool is too, and it keeps a full-catalog run kind to the anonymous
rate budget. Every string a result carries comes from the gateway's own
projection: printing it never leaks a source name or gateway token.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from core.logging import LOG
from services.http import HttpService

TEST_TIMEOUT = 120.0  # reference tool: 120s per attempt
RATE_RETRY_DELAY = 4.0  # reference: one retry, 4s later
PROMPT = "Reply with exactly: OK"
MAX_TOKENS = 16

OK = "OK"
RATE = "RATE"
FAIL = "FAIL"
EMPTY = "EMPTY"


def build_payload(model_id: str, endpoint_type: str) -> tuple[str, dict]:
    """(path relative to /v1, request body) chosen from the row's OWN
    endpoint_type — the reference tool drives all three shapes this way."""
    endpoint = str(endpoint_type or "chat.completion").lower()
    if "systemone" in endpoint:
        return (
            "/systemone",
            {
                "model": model_id,
                "state": "ping",
                "questions": {"test": {"type": "noul", "instructions": PROMPT}},
            },
        )
    if "response" in endpoint:
        return (
            "/responses",
            {"model": model_id, "input": PROMPT, "stream": False, "max_tokens": MAX_TOKENS},
        )
    return (
        "/chat/completions",
        {
            "model": model_id,
            "messages": [{"role": "user", "content": PROMPT}],
            "stream": False,
            "max_tokens": MAX_TOKENS,
        },
    )


def classify(status: int | None, text: str, *, transport_error: bool = False) -> str:
    """The reference classifier, verbatim: RATE only for 429, EMPTY for a 200
    with nothing usable, everything else FAIL."""
    if transport_error:
        return FAIL
    if status == 429:
        return RATE
    if status != 200:
        return FAIL
    if text.strip():
        return OK
    return EMPTY


def text_of(payload: object) -> str:
    """Reference extraction order: chat content -> Responses output_text ->
    output[] walk. Empty string means "200 but nothing usable" (EMPTY)."""
    if not isinstance(payload, dict):
        return ""
    try:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text
        if isinstance(output_text, list):
            # The Responses API ships output_text as a list of string parts.
            parts = []
            for part in output_text:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(str(part.get("text", "")))
            return "".join(parts)
        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []

            def _walk(items: list) -> None:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    inner = item.get("content")
                    if isinstance(inner, list):
                        _walk(inner)

            _walk(output)
            return "".join(parts)
    except Exception:
        return ""
    return ""


async def test_model(
    http: HttpService,
    base_url: str,
    model_id: str,
    endpoint_type: str = "",
) -> dict:
    """One probe (+ one 429 retry after 4s). `base_url` includes /v1."""
    path, body = build_payload(model_id, endpoint_type)
    started = time.monotonic()
    status: int | None = None
    payload: object = None
    failure = ""
    for attempt in range(2):
        try:
            response = await http.client.post(base_url + path, json=body, timeout=TEST_TIMEOUT)
        except httpx.HTTPError as exc:
            failure = str(exc)[:120]
            break
        status = response.status_code
        if status == 429 and attempt == 0:
            await asyncio.sleep(RATE_RETRY_DELAY)
            continue
        try:
            payload = response.json()
        except Exception:
            payload = None
        break
    ms = int((time.monotonic() - started) * 1000)
    text = text_of(payload)
    verdict = classify(status, text, transport_error=bool(failure) and status is None)
    snippet = (
        text.strip().replace(chr(10), " ")[:80]
        or failure
        or (f"HTTP {status}" if status is not None else "")
    )
    LOG.debug("bench %s -> %s (%sms)", model_id, verdict, ms)
    return {"id": model_id, "verdict": verdict, "ms": ms, "snippet": snippet}


async def retest(
    http: HttpService,
    base_url: str,
    rows: list[dict],
    on_row=None,
    stop_event=None,
) -> list[dict]:
    """Serial sweep over `rows` (filtering happens in the controller).

    `on_row(result, done, total)` fires after every probe — called on the
    portal thread; the controller marshals to the UI. `stop_event` is checked
    between rows so Stop takes effect within one probe.
    """
    results: list[dict] = []
    total = len(rows)
    for done, row in enumerate(rows, start=1):
        if stop_event is not None and stop_event.is_set():
            LOG.info("retest sweep stopped after %d/%d", done - 1, total)
            break
        model_id = str(row.get("id") or "")
        if not model_id:
            continue
        result = await test_model(
            http,
            base_url,
            model_id,
            str(row.get("endpoint_type") or ""),
        )
        results.append(result)
        if on_row is not None:
            try:
                on_row(result, done, total)
            except Exception as exc:  # progress must never kill the sweep
                LOG.debug("retest progress callback failed: %s", exc)
    return results
