"""Per-conversation kani sessions: the reason "stop the generation first"
gates could be deleted.

- Two conversations turn in PARALLEL on the one portal (kani's own model).
- Histories, engines and busy flags are session-scoped.
- Saving pins the conversation id, so a background turn lands in its own file
  even after the user switched away.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import openai
from test_boot_smoke import boot_page

from core.settings import AppSettings
from core.state import state
from services import history
from services.agent import _TURN_RESERVED, AgentService

__all__ = ["boot_page"]


def _agent(monkeypatch, handler) -> AgentService:
    from kani.engines.openai import OpenAIEngine

    def factory(settings: AppSettings, model: str) -> OpenAIEngine:
        client = openai.AsyncOpenAI(
            base_url="http://gateway.test/v1",
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return OpenAIEngine(
            client=client,
            model=model,
            api_type="chat_completions",
            max_context_size=4096,
        )

    monkeypatch.setattr(state, "gateway_running", True)
    monkeypatch.setattr(state, "gateway_base_url", "http://gateway.test/v1")
    monkeypatch.setattr(state, "model", "mimo-v2.6-flash-free")
    return AgentService(AppSettings(), engine_factory=factory)


def _sse_handler(request: httpx.Request) -> httpx.Response:
    body = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "hi there"}}]})
        + "\n\n"
        + "data: "
        + json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        + "\n\n"
        + "data: [DONE]\n\n"
    ).encode()
    return httpx.Response(200, content=body)


def test_two_conversations_turn_in_parallel(monkeypatch) -> None:
    """A busy conversation A must not block a send in conversation B."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        assert agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="a")
        assert agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="b")

        finished = {"a": threading.Event(), "b": threading.Event()}

        def _send(conv: str) -> bool:
            return agent.start_turn(
                "hello",
                on_delta=lambda c: None,
                on_done=lambda msg, usage: finished[conv].set(),
                on_error=lambda kind, text: finished[conv].set(),
                conv_id=conv,
            )

        assert _send("a") is True
        assert agent.session_busy("a") is True
        # The whole point: conversation B dispatches while A is streaming.
        assert _send("b") is True, "a turn in conversation A blocked conversation B"
        assert agent.session_busy("b") is True

        assert finished["a"].wait(15), "turn A never finished"
        assert finished["b"].wait(15), "turn B never finished"
        assert agent.session_busy("a") is False and agent.session_busy("b") is False
        # Each session kept its own transcript.
        assert len(agent.session_kani("a").chat_history) == 2
        assert len(agent.session_kani("b").chat_history) == 2
    finally:
        agent.stop()


def test_same_conversation_still_refuses_a_second_turn(monkeypatch) -> None:
    """One kani runs one turn at a time — same-conversation double send is
    still refused (that is kani's own session rule)."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="a")
        done = threading.Event()
        assert agent.start_turn(
            "one",
            on_delta=lambda c: None,
            on_done=lambda m, u: done.set(),
            on_error=lambda k, t: done.set(),
            conv_id="a",
        )
        assert (
            agent.start_turn(
                "two",
                on_delta=lambda c: None,
                on_done=lambda m, u: done.set(),
                on_error=lambda k, t: done.set(),
                conv_id="a",
            )
            is False
        )
        assert done.wait(15)
    finally:
        agent.stop()


def test_session_histories_and_resets_are_isolated(monkeypatch) -> None:
    """ensure_kani on conversation B must never see or clear A's history."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        kani_a = agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="a")
        from kani import ChatMessage

        kani_a.chat_history.append(ChatMessage.user("kept for a"))
        kani_b = agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="b")
        assert kani_b is not kani_a
        assert kani_b.chat_history == [], "new session inherited A's history"
        assert [str(m.text) for m in agent.session_kani("a").chat_history] == ["kept for a"]

        agent.reset_conversation("a")
        assert agent.session_kani("a") is None, "reset must drop the session"
        assert agent.session_kani("b") is not None, "reset touched the wrong session"
    finally:
        agent.stop()


def test_reservation_and_stop_are_session_scoped(monkeypatch) -> None:
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        sess_a = agent._session("a")
        sess_a.current = _TURN_RESERVED
        assert agent.session_busy("a") is True
        assert agent.session_busy("b") is False
        agent.stop_turn("a")
        assert agent.session_busy("a") is False
    finally:
        agent.stop()


def test_save_conversation_pins_the_conversation_id(boot_page, monkeypatch) -> None:
    """A background turn's save must land in ITS file, not the active one."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    previous_active = state.active_conversation
    try:
        kani_a = agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="conv-a")
        kani_b = agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="conv-b")
        from kani import ChatMessage

        kani_a.chat_history.append(ChatMessage.user("question for a"))
        kani_b.chat_history.append(ChatMessage.user("question for b"))

        # The user switched to A; B's turn finishes LAST.
        state.active_conversation = "conv-a"
        agent.active_conv = "conv-a"
        assert history.save_conversation(agent, "conv-b") is True
        data = json.loads(history.conversation_path("conv-b").read_text(encoding="utf-8"))
        texts = [m.get("content") for m in data["chat_history"]]
        assert "question for b" in texts, f"conv-b.json got the wrong transcript: {texts}"
    finally:
        agent.stop()
        state.active_conversation = previous_active


def test_session_busy_flag_drives_the_view(monkeypatch) -> None:
    """busy reflects the ACTIVE conversation only."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        agent.active_conv = "a"
        agent._session("a").current = _TURN_RESERVED
        assert agent.busy is True
        agent.active_conv = "b"
        assert agent.busy is False, "another conversation's turn leaked into busy"
    finally:
        agent.stop()


def test_new_and_opened_conversations_share_the_observable_list(boot_page) -> None:
    """flet only repaints on mutations of the OBSERVABLE list.

    `state.messages` is wrapped into an ObservableList on assignment, and the
    UI subscribes to that wrapper. Storing the pre-wrap list in _conv_messages
    leaves the streaming worker writing a list nobody watches: content lands
    but the view never repaints until some other observable is assigned —
    the owner's "I send in a new chat while one is running and only see the
    spinner, then I switch tabs and it's done". The map must hold exactly the
    object the view renders.
    """
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    # New chat.
    controller.methods.new_conversation()
    cid = state.active_conversation
    assert controller._conv_messages[cid] is state.messages, (
        "new conversation: map holds the raw list, the view holds the wrapper"
    )

    # Messages written through the map's list MUST notify the observable.
    # (Observable.__listeners is a WeakSet: keep a strong ref or the listener
    # is collected before the mutation.)
    events: list = []
    listener = lambda _sender, _field: events.append(_field)  # noqa: E731
    keep_alive: list = [listener]  # Observable listeners live in a WeakSet
    state.subscribe(keep_alive[0])
    controller._conv_messages[cid].append({"role": "user", "content": "hi"})
    assert keep_alive, "test bookkeeping"
    assert events, "appending to a conversation's list produced no repaint event"
    assert state.messages[-1]["content"] == "hi"
    # item assignment (what flush() does) must notify too
    controller._conv_messages[cid].append({"role": "assistant", "content": "partial"})
    controller._conv_messages[cid][-1] = {"role": "assistant", "content": "more"}
    assert len(events) >= 3, events

    # Opened conversation: a fresh list handed over by _conversation_loaded.
    controller._conversation_loaded(True, "opened123456", [{"role": "user", "content": "old"}])
    assert controller._conv_messages["opened123456"] is state.messages, (
        "opened conversation: map holds the raw list, the view holds the wrapper"
    )


def test_turn_settle_time_is_bounded(monkeypatch) -> None:
    """Sanity: a full session turn settles well under the ceiling constant."""
    agent = _agent(monkeypatch, _sse_handler)
    agent.start()
    try:
        agent.ensure_kani("mimo-v2.6-flash-free", "be brief", conv_id="a")
        done = threading.Event()
        started_at = time.monotonic()
        assert agent.start_turn(
            "hello",
            on_delta=lambda c: None,
            on_done=lambda m, u: done.set(),
            on_error=lambda k, t: done.set(),
            conv_id="a",
        )
        assert done.wait(15)
        assert time.monotonic() - started_at < 15
    finally:
        agent.stop()
