"""Transcript-guard regression: dangling tool claims must never poison turns."""

from __future__ import annotations

from kani import ChatMessage
from kani.models import FunctionCall, ToolCall


def _assistant_with_call(call_id: str) -> ChatMessage:
    tc = ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name="GitHub.get_me", arguments="{}"),
    )
    return ChatMessage.assistant("checking", tool_calls=[tc])


def test_paired_transcript_survives() -> None:
    from services.agent import _drop_orphan_function_messages

    msgs = [
        ChatMessage.user("who owns kiri?"),
        _assistant_with_call("call_1"),
        ChatMessage.function("GitHub.get_me", "Nwokike", tool_call_id="call_1"),
    ]
    assert _drop_orphan_function_messages(msgs) == msgs


def test_dangling_claim_detected() -> None:
    from services.agent import _pending_tool_call_ids

    msgs = [
        ChatMessage.user("who owns kiri?"),
        _assistant_with_call("call_85ad"),
    ]
    assert _pending_tool_call_ids(msgs) == {"call_85ad"}


def test_satisfied_claim_not_pending() -> None:
    from services.agent import _pending_tool_call_ids

    msgs = [
        ChatMessage.user("hi"),
        _assistant_with_call("call_1"),
        ChatMessage.function("GitHub.get_me", "Nwokike", tool_call_id="call_1"),
    ]
    assert _pending_tool_call_ids(msgs) == set()


def test_orphan_result_dropped_by_id() -> None:
    from services.agent import _drop_orphan_function_messages

    msgs = [
        ChatMessage.user("hi"),
        ChatMessage.function("GitHub.get_me", "stale result", tool_call_id="call_gone"),
        ChatMessage.user("next question"),
    ]
    kept = _drop_orphan_function_messages(msgs)
    assert all(getattr(m, "tool_call_id", None) != "call_gone" for m in kept)
    assert len(kept) == 2


def test_preflight_evicts_dangling_claim(monkeypatch) -> None:
    """The exact owner log: assistant claimed call_85ad, no FUNCTION reply,
    every later turn failed until reset. Preflight must evict the claim."""
    from core.settings import AppSettings
    from core.state import state
    from services.agent import AgentService

    monkeypatch.setattr(state, "gateway_running", True)
    agent = AgentService(AppSettings())
    agent.start()
    try:
        errors: list = []
        done = __import__("threading").Event()

        class _Manager:
            role = "assistant"

            def __init__(self, message):
                self._message = message
                self.tool_calls = None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def message(self):
                return self._message

        seen_claims: list = []

        class _Kani:
            def __init__(self):
                self.chat_history = [
                    ChatMessage.user("who owns this repo?"),
                    _assistant_with_call("call_85ad"),
                    ChatMessage.user("try again"),
                ]

            def full_round_stream(self, *a, **k):
                async def _gen():
                    # If the dangling claim reaches the provider, it raises
                    # the exact owner-log error instead of yielding.
                    for m in self.chat_history:
                        if getattr(m, "tool_calls", None):
                            ids = [t.id for t in m.tool_calls]
                            seen_claims.extend(ids)
                            raise RuntimeError(
                                "Encountered a 'user' message but expected a "
                                f"FUNCTION message to satisfy {set(ids)}"
                            )
                    yield _Manager(ChatMessage.assistant("clean reply"))

                return _gen()

        agent._kani = _Kani()  # type: ignore[assignment]
        agent._model = "m"
        agent._system = "s"
        import services.agent as agent_mod

        monkeypatch.setattr(agent_mod, "estimate_turn_tokens", lambda **k: (10, False))
        started = agent.start_turn(
            "try again",
            on_delta=lambda c: None,
            on_done=lambda m, u: done.set(),
            on_error=lambda kind, text: (errors.append((kind, text)), done.set()),
        )
        assert started is True
        assert done.wait(15), "turn did not settle after claim eviction"
        assert errors == [], f"poisoned turn surfaced errors: {errors}"
        assert "call_85ad" not in seen_claims, "dangling claim reached the provider"
        remaining = [m for m in agent._kani.chat_history if getattr(m, "tool_calls", None)]
        assert remaining == [], "dangling claim survived preflight"
    finally:
        agent.stop()
