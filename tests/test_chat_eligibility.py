"""Chat picker eligibility + endpoint vocabulary.

The Server screen keeps the FULL gateway catalog (honest mirror); only the
chat picker filters. Both non-chat endpoint families stay out, for reasons
verified live on 2026-09-24 rather than assumed:

  * `systemone` — not an OpenAI-shaped body at all.
  * `response`   — the catalog advertises it, but BOTH muse-spark-contributor
    models answer 401 "Model ... is not supported", and kani 1.10's Responses
    path raises AttributeError ('dict' has no attribute 'model_copy') in
    both predict() and stream().

`api_type` still maps them, so the day either is fixed they work unchanged.
"""

from core.catalog import (
    AUTO_MODEL_ID,
    api_type_for,
    chat_models,
    chat_support_note,
    endpoint_label,
    is_auto,
    is_chat_eligible,
    model_label,
    rate_hint_label,
)


def test_active_chat_model_is_eligible() -> None:
    assert is_chat_eligible(
        {"id": "mimo-v2.6-flash", "status": "active", "endpoint_type": "chat.completion"},
    )
    assert is_chat_eligible({"id": "auto", "status": "active", "endpoint_type": "chat.completion"})


def test_non_chat_endpoints_are_ineligible() -> None:
    # systemone replies with a non-OpenAI body.
    assert not is_chat_eligible(
        {"id": "jev-1.13", "status": "active", "endpoint_type": "systemone"},
    )
    # response: advertised, but 401 upstream AND broken in kani today.
    assert not is_chat_eligible(
        {"id": "muse-spark-1.2-contributor", "status": "active", "endpoint_type": "response"},
    )


def test_excluded_models_explain_why_on_the_server_tab() -> None:
    """Hiding them silently would look like a bug; the Server tab says why."""
    note = chat_support_note(
        {"id": "muse-spark-1.2-contributor", "status": "active", "endpoint_type": "response"},
    )
    assert "Not usable in chat" in note
    assert "systemone" not in chat_support_note(
        {"id": "ok", "status": "active", "endpoint_type": "chat.completion"},
    )


def test_api_type_follows_the_endpoint() -> None:
    assert api_type_for({"endpoint_type": "chat.completion"}) == "chat_completions"
    assert api_type_for({"endpoint_type": "response"}) == "responses"
    # Unknown shapes defer to kani rather than guessing.
    assert api_type_for({"endpoint_type": "systemone"}) is None
    assert api_type_for({}) is None


def test_endpoints_are_spelled_out_in_full() -> None:
    assert endpoint_label({"endpoint_type": "chat.completion"}) == "Chat completion"
    assert endpoint_label({"endpoint_type": "response"}) == "Response"
    assert endpoint_label({"endpoint_type": "systemone"}) == "System one"
    # Never the truncated "chat" the Server screen used to show.
    assert endpoint_label({"endpoint_type": "chat.completion"}) != "chat"


def test_auto_is_recognised_and_sorts_first() -> None:
    auto = {"id": AUTO_MODEL_ID, "status": "active", "endpoint_type": "chat.completion"}
    zebra = {"id": "zebra", "status": "active", "endpoint_type": "chat.completion"}
    alpha = {"id": "alpha", "status": "active", "endpoint_type": "chat.completion"}
    assert is_auto(auto)
    assert not is_auto(alpha)
    assert [m["id"] for m in chat_models([zebra, alpha, auto])] == [AUTO_MODEL_ID, "alpha", "zebra"]
    assert model_label(auto) == "Auto"
    assert model_label(alpha) == "alpha"


def test_auto_is_ineligible_when_the_router_reports_it_down() -> None:
    assert not is_chat_eligible({"id": AUTO_MODEL_ID, "status": "failed"})


def test_rate_hint_is_surfaced_for_the_user() -> None:
    model = {
        "id": "m",
        "rate_hint": {"tier": "generous", "approx_per_hour": 200, "label": "Free tier"},
    }
    assert rate_hint_label(model) == "Free tier"
    # A hint with only a tier/hour still produces something readable.
    assert "/hour" in rate_hint_label({"rate_hint": {"tier": "standard", "approx_per_hour": 60}})
    # The rotating model explains itself.
    assert "rotates" in rate_hint_label(
        {"rate_hint": {"label": "Free tier, rotates across available models"}},
    )
    assert rate_hint_label({"id": "m"}) == ""


def test_non_active_statuses_are_ineligible() -> None:
    for status in ("untested", "failed", "slow", "rate limited"):
        assert not is_chat_eligible(
            {"id": "m", "status": status, "endpoint_type": "chat.completion"},
        ), status


def test_missing_fields_fail_open_for_unknown_gateways() -> None:
    # No status/endpoint keys (adopted foreign gateway) -> allow, we can't know.
    assert is_chat_eligible({"id": "mystery-model"})
    # Explicit endpoint chat shorthand
    assert is_chat_eligible({"id": "m", "status": "active", "endpoint_type": "chat"})


# ── rate-limit messaging ─────────────────────────────────────────────────────

from core.catalog import rate_limit_advice  # noqa: E402

_CATALOG = [
    {
        "id": "auto",
        "status": "active",
        "endpoint_type": "chat.completion",
        "rate_hint": {"label": "Free tier, rotates across available models"},
    },
    {
        "id": "capped-60",
        "status": "active",
        "endpoint_type": "chat.completion",
        "latency_ms": 1200,
        "rate_hint": {
            "tier": "standard",
            "approx_per_hour": 60,
            "label": "Free tier, roughly 60 requests/hour",
        },
    },
    {
        "id": "generous-200",
        "status": "active",
        "endpoint_type": "chat.completion",
        "latency_ms": 310,
        "rate_hint": {"tier": "generous", "approx_per_hour": 200, "label": "Free tier"},
    },
    {"id": "dead-model", "status": "failed", "endpoint_type": "chat.completion"},
]


def test_rate_limit_message_quotes_the_models_own_hint() -> None:
    msg = rate_limit_advice("capped-60", _CATALOG)
    assert "60 requests/hour" in msg
    assert "Rate limited" in msg


def test_rate_limit_message_names_a_usable_alternative() -> None:
    msg = rate_limit_advice("capped-60", _CATALOG)
    # dead models are never suggested
    assert "dead-model" not in msg
    assert "generous-200" in msg or "auto" in msg


def test_rate_limit_message_is_specific_for_auto() -> None:
    msg = rate_limit_advice("auto", _CATALOG)
    assert "rotates" in msg
    assert "Rate limited" in msg


def test_rate_limit_message_degrades_gracefully() -> None:
    # Unknown model, empty catalog: still a sentence, never an exception.
    assert rate_limit_advice("nope", []).startswith("Rate limited")
    assert rate_limit_advice("", _CATALOG).startswith("Rate limited")


def test_status_label_translates_untested_to_rate_limited() -> None:
    """Owner rule: the wire word `untested` is shown as "rate limited" —
    never raw, never "degraded"."""
    from core.catalog import status_label

    assert status_label("untested") == "rate limited"
    assert status_label("UNTESTED") == "rate limited"
    assert status_label("slow") == "slow"
    assert status_label("failed") == "failed"
    assert status_label("active") == "active"
    assert status_label(None) == "unavailable"
    assert status_label("") == "unavailable"


def test_malformed_rate_metadata_never_raises() -> None:
    """Remote metadata is untrusted: "lots" used to raise int() inside
    rate_limit_advice's `except RateLimitError` handler (a SIBLING catch) —
    the turn died with no error row at all."""
    from core.catalog import rate_hint_label, rate_limit_suggestion

    broken = {
        "id": "weird-model",
        "rate_hint": {"approx_per_hour": "lots", "tier": "free"},
        "latency_ms": "fast",
    }
    label = rate_hint_label(broken)
    assert isinstance(label, str)

    suggestion = rate_limit_suggestion("other-model", [broken])
    assert isinstance(suggestion, str)

    from core.catalog import rate_limit_advice

    advice = rate_limit_advice("other-model", [broken])
    assert isinstance(advice, str) and advice
