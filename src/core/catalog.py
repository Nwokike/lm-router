"""Model catalog vocabulary: endpoints, eligibility, rate hints.

The Kiri Router serves an OpenAI-shaped catalog plus its own `auto` model and
per-model `rate_hint` metadata. Everything the UI needs to interpret those
rows lives here so the Server screen, the chat picker and the agent all agree
on what a model *is*.

Endpoint names are spelled out in full for users ("Chat completion", not
"chat"); the raw `endpoint_type` stays available for protocol decisions.
"""

from __future__ import annotations

from typing import Any

# The router's rotating model. It is a real upstream entry, not a client-side
# heuristic: the gateway ranks healthy candidates, prefers tool-capable models
# when a request carries tools, and rotates stickily per conversation.
AUTO_MODEL_ID = "auto"

# endpoint_type -> (human label, kani api_type)
#   kani's OpenAIEngine speaks either the Chat Completions shape or the
#   Responses shape; picking the wrong one yields empty/unparsable replies.
ENDPOINTS: dict[str, tuple[str, str | None]] = {
    "chat.completion": ("Chat completion", "chat_completions"),
    "chat.completions": ("Chat completion", "chat_completions"),
    "chat": ("Chat completion", "chat_completions"),
    "response": ("Response", "responses"),
    "responses": ("Response", "responses"),
    "systemone": ("System one", None),
}

UNKNOWN_ENDPOINT = ("Chat completion", "chat_completions")

# Endpoints verified to produce a usable chat reply end-to-end, 2026-09-24:
#   chat.completion -> OK
#   response        -> gateway advertises it, but BOTH muse-spark-contributor
#                      models answer 401 "Model ... is not supported", and kani
#                      1.10's Responses path raises
#                      AttributeError: 'dict' object has no attribute
#                      'model_copy' in both predict() and stream().
#   systemone       -> not an OpenAI-shaped body at all.
# So both stay out of the chat picker and are labelled honestly on the Server
# screen. Revisit if the router starts serving them AND kani's Responses path
# is fixed; the api_type mapping above is already in place for that day.
CHAT_CAPABLE_ENDPOINTS = frozenset({"chat.completion", "chat.completions", "chat"})


def endpoint_label(model: dict[str, Any] | None) -> str:
    """Full, human-readable endpoint name ("Chat completion")."""
    if not model:
        return UNKNOWN_ENDPOINT[0]
    raw = str(model.get("endpoint_type") or model.get("endpoint") or "").strip().lower()
    return ENDPOINTS.get(raw, UNKNOWN_ENDPOINT)[0]


def api_type_for(model: dict[str, Any] | None) -> str | None:
    """kani api_type for a catalog row, or None when the shape is unknown.

    None means "let kani decide" (it warns and falls back), which is the safe
    choice for endpoints we do not recognise — notably `systemone`, whose body
    is not OpenAI-shaped at all.
    """
    if not model:
        return None
    raw = str(model.get("endpoint_type") or model.get("endpoint") or "").strip().lower()
    return ENDPOINTS.get(raw, (None, None))[1]


def is_auto(model: dict[str, Any] | None) -> bool:
    return bool(model) and str(model.get("id") or "").lower() == AUTO_MODEL_ID


def is_active(model: dict[str, Any] | None) -> bool:
    if not model:
        return False
    status = model.get("status")
    return status is None or str(status).lower() == "active"


def is_chat_eligible(model: dict[str, Any]) -> bool:
    """True when the chat picker may offer this model.

    Eligible means: the model exists, is ACTIVE right now, and speaks a shape
    we have VERIFIED end-to-end (see CHAT_CAPABLE_ENDPOINTS). `auto` is always
    eligible when active — the router guarantees the composed answer is
    chat-shaped.
    """
    if not model or not model.get("id"):
        return False
    if not is_active(model):
        return False
    if is_auto(model):
        return True
    # An endpoint we have no mapping for is not something we can render.
    raw = str(model.get("endpoint_type") or model.get("endpoint") or "").strip().lower()
    if not raw:
        return True
    return raw in CHAT_CAPABLE_ENDPOINTS


def chat_models(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat-eligible rows, `auto` first, then the rest alphabetically."""
    eligible = [m for m in catalog or [] if is_chat_eligible(m)]
    auto = [m for m in eligible if is_auto(m)]
    rest = sorted(
        (m for m in eligible if not is_auto(m)),
        key=lambda m: str(m.get("id") or "").lower(),
    )
    return auto + rest


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return default


def rate_hint_label(model: dict[str, Any] | None) -> str:
    """The gateway's own one-line rate-limit copy (its label, verbatim).

    D5: rate limits surface only as the approximate profile the GATEWAY
    publishes — never a policy number we synthesize ourselves, and never a
    parse of untrusted metadata (a router publishing "lots" once raised
    inside agent.py's RateLimitError handler). Label or nothing.
    """
    if not model:
        return ""
    hint = model.get("rate_hint")
    if not isinstance(hint, dict):
        return ""
    return str(hint.get("label") or "").strip()


def model_label(model: dict[str, Any]) -> str:
    """Picker/row label: the id, with a marker for the rotating model."""
    if not model:
        return ""
    model_id = str(model.get("id") or "")
    return "Auto" if is_auto(model) else model_id


def status_label(status: object) -> str:
    """Gateway status word -> user-facing label.

    The gateway vocabulary is active|untested|slow|failed; `untested` means
    tried-and-capped, which the owner says to call "rate limited" — never the
    raw wire word, and never "degraded".
    """
    word = str(status or "").strip().lower()
    if not word:
        return "unavailable"
    return "rate limited" if word == "untested" else word


def _rank_alternatives(model_id: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alternatives = [
        m
        for m in chat_models([m for m in catalog or [] if isinstance(m, dict)])
        if str(m.get("id") or "") != model_id
    ]

    # Prefer an alternative whose own hint is not a low cap, so the suggestion
    # is likely to work right now.
    def _cap(m: dict) -> tuple[int, float, str]:
        per_hour = (m.get("rate_hint") or {}).get("approx_per_hour")
        try:
            cap = int(per_hour) if per_hour is not None else 10**9
        except TypeError, ValueError:
            cap = 10**9
        latency = _as_float(m.get("latency_ms"), 10**6)
        return (0 if cap >= 200 else 1, latency, str(m.get("id") or ""))

    alternatives.sort(key=_cap)
    return alternatives


def rate_limit_suggestion(model_id: str, catalog: list[dict[str, Any]]) -> str:
    """First-choice alternative model id for a rate-limited model ("" = none).

    Pairs with rate_limit_advice's "Try X instead." text: the message names
    the alternatives, this hands the UI a tappable id for the action button.
    """
    ranked = _rank_alternatives(model_id, catalog)
    return str(ranked[0].get("id") or "") if ranked else ""


def rate_limit_advice(model_id: str, catalog: list[dict[str, Any]]) -> str:
    """A specific, reassuring message for a model that just rate limited.

    "Rate limited. Try another model." reads like something is broken. The
    router already publishes a per-model `rate_hint` (tier, requests/hour,
    label) and a pool of healthy alternatives, so the message can say what the
    cap is and name a model that is not capped.
    """
    row = next((m for m in catalog or [] if str(m.get("id") or "") == model_id), None)
    hint = rate_hint_label(row) if row else ""

    if str(model_id).lower() == AUTO_MODEL_ID:
        # `auto` composes the healthy pool, so a cap here is a pool-wide one.
        if hint:
            return (
                f"Rate limited right now. {hint}. Try again shortly, or pick another model below."
            )
        return "Rate limited right now. Try again shortly, or pick a model below."

    ranked = _rank_alternatives(model_id, catalog)
    suggestion = ""
    if ranked:
        names = ", ".join(str(m.get("id")) for m in ranked[:2])
        suggestion = f" Try {names} instead."

    if hint:
        return f"Rate limited. {hint}.{suggestion}" if suggestion else f"Rate limited. {hint}."
    return (
        f"Rate limited by this model.{suggestion}"
        if suggestion
        else ("Rate limited by this model. Try again in a moment.")
    )


def chat_support_note(model: dict[str, Any]) -> str:
    """Why a catalog row is (or is not) usable in chat — shown on Server.

    Honest labelling matters here: the catalog advertises `response` and
    `systemone` models, but neither currently produces a usable chat reply.
    Hiding them silently would look like a bug; saying why is useful.
    """
    if not model:
        return ""
    if is_chat_eligible(model):
        return ""
    raw = str(model.get("endpoint_type") or model.get("endpoint") or "").strip().lower()
    if raw in ("response", "responses"):
        return "Not usable in chat yet: upstream rejects it and kani's Responses path is broken"
    if raw == "systemone":
        return "Not usable in chat: this endpoint does not return an OpenAI-shaped reply"
    if not is_active(model):
        return f"Currently {status_label(model.get('status'))}"
    return ""
