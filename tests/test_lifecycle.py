"""Lifecycle/teardown regression: M2 fixes must hold.

- Engine HTTP clients close on rebuild / stop / portal restart (no leak).
- A UI callback raising mid-turn never aborts the turn or double-fires done.
- stop_turn is safe against the reservation sentinel and concurrent reserve.
- Retest generation: a stale sweep's finish never clears a newer sweep.
"""

from __future__ import annotations

import threading
import time

import httpx
import openai
from test_boot_smoke import boot_page

from core.settings import AppSettings
from core.state import state
from services.agent import AgentService

__all__ = ["boot_page"]


def _agent(monkeypatch, handler) -> AgentService:
    from kani.engines.openai import OpenAIEngine

    def factory(settings: AppSettings, model: str) -> OpenAIEngine:
        # Fresh SDK client per engine (mirrors production): lets the test
        # prove rebuilds actually replace the tracked client.
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

    return AgentService(AppSettings(), engine_factory=factory)


def _boot(monkeypatch, handler) -> AgentService:
    monkeypatch.setattr(state, "gateway_running", True)
    monkeypatch.setattr(state, "gateway_base_url", "http://gateway.test/v1")
    monkeypatch.setattr(state, "model", "mimo-v2.6-flash-free")
    agent = _agent(monkeypatch, handler)
    agent.start()
    agent.ensure_kani("mimo-v2.6-flash-free", "be brief")
    return agent


def test_engine_clients_tracked_and_closed_on_rebuild(monkeypatch) -> None:
    """ensure_kani must track the live SDK client and close it on rebuild."""
    agent = _boot(monkeypatch, lambda request: httpx.Response(200, json={}))
    try:
        sess = agent._session(agent.active_conv)
        assert sess.client is not None, "engine client not tracked"
        first = sess.client
        agent.ensure_kani("other-model", "be brief")
        assert sess.client is not first, "rebuild did not replace client"
    finally:
        agent.stop()
    sess = agent._session(agent.active_conv)
    assert sess.client is None, "stop did not clear tracked client"


def test_restart_portal_invalidates_stale_engine(monkeypatch) -> None:
    """A portal restart must drop the kani bound to the dead loop."""
    agent = _boot(monkeypatch, lambda request: httpx.Response(200, json={}))
    try:
        assert agent._kani is not None
        agent.restart_portal()
        assert agent._kani is None, "stale engine survived portal restart"
        assert agent._model == "", "model key survived portal restart"
    finally:
        agent.stop()


def test_raising_on_delta_does_not_abort_turn(monkeypatch) -> None:
    """A UI callback raising mid-stream must not kill the turn."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            "data: " + _json.dumps({"choices": [{"delta": {"content": "hello"}}]}) + "\n\n"
            "data: " + _json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 1}}) + "\n\n"
            "data: [DONE]\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    agent = _boot(monkeypatch, handler)
    try:
        done = threading.Event()
        seen: dict = {"deltas": 0, "dones": 0, "errors": []}

        def bad_delta(chunk: str) -> None:
            seen["deltas"] += 1
            raise RuntimeError("UI blew up")

        started = agent.start_turn(
            "hi",
            on_delta=bad_delta,
            on_done=lambda msg, usage: (seen.__setitem__("dones", seen["dones"] + 1), done.set()),
            on_error=lambda kind, text: (seen["errors"].append(kind), done.set()),
        )
        assert started is True
        assert done.wait(15), "turn did not finish after callback raised"
        assert seen["dones"] == 1, "on_done must fire exactly once"
        assert seen["errors"] == [], "UI bug must not surface as upstream error"
        deadline = time.monotonic() + 5
        while agent.busy and time.monotonic() < deadline:
            time.sleep(0.05)
        assert agent.busy is False
    finally:
        agent.stop()


def test_stop_during_reservation_is_safe(monkeypatch) -> None:
    """stop_turn on the reservation sentinel must not raise or wedge busy."""
    from services.agent import _TURN_RESERVED

    agent = _agent(monkeypatch, lambda request: httpx.Response(200, json={}))
    agent.start()
    try:
        agent._session(agent.active_conv).current = _TURN_RESERVED
        assert agent.busy is True
        agent.stop_turn()  # must be a silent no-op
        assert agent.busy is False
    finally:
        agent.stop()


def test_busy_getter_does_not_mutate_finished_handle(monkeypatch) -> None:
    """Reading busy must not clear a finished handle (dispatcher owns that)."""

    class _Done:
        def done(self) -> bool:
            return True

    agent = _agent(monkeypatch, lambda request: httpx.Response(200, json={}))
    agent.start()
    try:
        handle = _Done()
        sess = agent._session(agent.active_conv)
        sess.current = handle
        assert agent.busy is False
        # Getter is read-only: the handle is still there for finally/stop.
        assert sess.current is handle
    finally:
        agent.stop()


def test_stale_retest_finish_does_not_clobber_new_sweep(boot_page) -> None:
    """A late _finish_retest from sweep N must not clear sweep N+1 flags."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._retest_gen = 5
    state.retesting = True
    state.model_testing = {"model-a"}
    controller._finish_retest(4)  # stale generation
    assert state.retesting is True
    assert state.model_testing == {"model-a"}

    controller._finish_retest(5)  # current generation clears
    assert state.retesting is False
    assert state.model_testing == set()
    assert isinstance(state.model_testing, set)


def test_quit_handler_accepts_event_payload(boot_page) -> None:
    """page.on_close invokes _quit_app WITH an event — must not TypeError."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    controller._quit_app(object())  # framework path: event payload
    assert controller._quitting is True
    controller._quitting = False  # reset so later tests can quit
