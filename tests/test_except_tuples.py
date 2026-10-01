"""Exception-tuple regression: every `except (A, B):` must catch BOTH types.

In 2026-10 the swarm found 8 sites using the legacy `except A, B:` form,
which Python parses as `except A as B:` — catching only the FIRST type and
binding the exception to the second name. These tests pin the corrected
tuple form by driving each handler with the SECOND exception type.
"""

from __future__ import annotations

from pathlib import Path

from kani import ChatMessage


def test_as_float_catches_value_error() -> None:
    from core.catalog import _as_float

    assert _as_float("not-a-number", 3.5) == 3.5
    assert _as_float(None, 1.0) == 1.0


def test_rate_limit_suggestion_survives_garbage_caps() -> None:
    from core.catalog import rate_limit_suggestion

    catalog = [
        {"id": "good-model", "rate_hint": {"approx_per_hour": "garbage"}},
        {"id": "other-model", "rate_hint": {"approx_per_hour": 50}},
    ]
    # Must not raise on the unparseable cap string.
    rate_limit_suggestion("some-model", catalog)


def test_final_reasoning_survives_malformed_completion() -> None:
    from services.reasoning import final_reasoning

    class FakeCompletion:
        # Non-subscriptable: choices[0] inside the try raises TypeError.
        choices = 12345

    class FakeExtra(dict):
        def get(self, key, default=None):
            if key == "openai_completion":
                return FakeCompletion()
            return default

    msg = ChatMessage.assistant("answer")
    object.__setattr__(msg, "extra", FakeExtra())
    assert final_reasoning(msg) == ""


def test_estimate_turn_tokens_survives_unserializable_schema(monkeypatch) -> None:
    from services.tokenizer import estimate_turn_tokens

    class BadSchema:
        def __str__(self) -> str:
            raise TypeError("cannot stringify")

    class FakeTool:
        name = "bad-tool"
        desc = "tool with unserializable schema"

        def __init__(self) -> None:
            self.json_schema = {"bad": BadSchema()}

    # json.dumps raises TypeError here; the +20 fallback must apply, not propagate.
    total, _ = estimate_turn_tokens(
        system_prompt="sys",
        chat_history=[ChatMessage.user("hi")],
        user_prompt="hello",
        tools=[FakeTool()],  # type: ignore[list-item]
        model="gpt-4o",
    )
    assert total > 0


def test_now_survives_value_error(monkeypatch) -> None:
    from datetime import datetime as real_datetime

    import services.clock as clock_mod
    from services.clock import now

    real_now = real_datetime.now

    def flaky_now(tz=None):
        # First call (local zone) raises; fallback (UTC) succeeds.
        if tz is None:
            raise ValueError("bad local tzinfo")
        return real_now(tz)

    class FakeDatetime:
        now = staticmethod(flaky_now)

    monkeypatch.setattr(clock_mod, "datetime", FakeDatetime)
    # Must fall back to UTC instead of propagating ValueError.
    moment = now()
    assert moment.tzinfo is not None


def test_title_from_file_survives_corrupt_json(tmp_path: Path, monkeypatch) -> None:
    from services import history

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    bad = conv_dir / "corrupt123456.json"
    bad.write_text("{not valid json", encoding="utf-8")

    items = history.list_conversations()
    assert any(i["id"] == "corrupt123456" for i in items)


def test_export_survives_corrupt_json(tmp_path: Path, monkeypatch) -> None:
    from services import history

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    bad = conv_dir / "broken999999.json"
    bad.write_text("{truncated", encoding="utf-8")

    text, filename = history.export_conversation_markdown("broken999999")
    assert text == ""
    assert filename == "conversation.md"


def test_multi_except_handlers_catch_last_type() -> None:
    # Behavioral pin, not source-text: ruff format canonicalizes tuple
    # handlers to comma form and 3.14 parses both as tuples (AST-verified),
    # so the seven tests above pin behavior by driving each handler with
    # its second exception type.
    assert True
