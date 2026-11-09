"""Dedicated asyncio loop on a daemon thread — the mini portal.

kani used to own a full anyio BlockingPortal for chat turns. The router only
needs a stable loop to run httpx coroutines on: HttpService's shared client
is loop-bound, so a fresh asyncio.run() per call would orphan it ("Event
loop is closed" on the second call). One daemon thread, one loop,
run_forever; callers marshal coroutines onto it from any thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any


class AsyncRunner:
    """Run async work off the Flet loop without kani's portal machinery."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="async-runner", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def _check(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("async runner is not running")
        return loop

    def run(self, coro_fn: Callable[..., Any], *args: Any, timeout: float = 300.0) -> Any:
        """Run coro_fn(*args) on the runner loop; block until it settles."""
        loop = self._check()
        return asyncio.run_coroutine_threadsafe(coro_fn(*args), loop).result(timeout=timeout)

    def spawn(self, coro_fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Fire-and-forget: schedule coro_fn(*args), return the future."""
        loop = self._check()
        return asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
