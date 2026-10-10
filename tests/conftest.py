"""Shared fixtures: pinned asyncio backend for the anyio pytest plugin."""

import os
from pathlib import Path

import pytest

# Pin tiktoken to the repo's existing BPE cache BEFORE core imports: the
# home-dir anchor for bare runs (core/__init__) would be empty here and force
# a 5MB network download during tests.
os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / "tiktoken_cache"),
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _stop_lingering_mcp_owners():
    """Tear down the controllers every test leaves running.

    `AppController.init()` spawns a REAL MCP owner on the agent portal whenever
    no UI loop exists (every boot/session test: the fake page has none). Nothing
    in those tests stops it, so the owner loops forever — every 0.5s it wakes,
    re-handshakes the seeded Exa/Parallel builtins and calls
    `tools_from_mcp_servers`, which later tests monkeypatch to hand out a fixed
    iterator. The leftovers consume those patches (StopIteration halfway through
    an unrelated test) and their portal threads slow the event loop enough to
    make timing asserts flake. That is two full-suite failures that pass in
    isolation and in smaller subsets.

    Track every controller built during a test and stop its owner + portal
    afterwards, so each test starts with a clean process.
    """
    import contextlib

    import main as main_mod

    created: list = []
    real_init = main_mod.AppController.__init__

    def _tracking_init(self, page):
        real_init(self, page)
        created.append(self)

    main_mod.AppController.__init__ = _tracking_init
    try:
        yield
    finally:
        main_mod.AppController.__init__ = real_init
        for controller in created:
            # Signal the owner to leave its loop. That is what stops the
            # pollution: it never calls tools_from_mcp_servers again once the
            # loop exits, and it wakes within one 0.5s park. The owner's own
            # finally (closing SDK contexts, 5s per abandoned client) is slow
            # and irrelevant, so it is deliberately NOT awaited — agent.stop()
            # here cost 13-21s per test. The portal is a daemon thread and dies
            # with the process, exactly as it did before this fixture existed.
            with contextlib.suppress(Exception):
                controller.services.mcp.request_stop()
            future = getattr(controller, "_mcp_future", None)
            if future is not None:
                # 1s is one full owner park (0.5s) plus margin: enough for the
                # loop to notice the stop event, and bounded so a stuck owner
                # cannot stretch the suite.
                with contextlib.suppress(Exception):
                    future.result(timeout=1.0)
