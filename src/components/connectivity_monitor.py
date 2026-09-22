"""Connectivity monitor (voicelm port): drives state.offline.

OS-level ft.Connectivity for instant changes plus an HTTP204 probe so
transient NONE reports on resume do not flash the banner, with a 5s poll
fallback for platforms where on_change is unreliable.
"""

import asyncio

import anyio
import flet as ft

from core.logging import LOG
from core.state import state

_CHECK_INTERVAL = 5
_CHECK_URL = "https://clients3.google.com/generate_204"
_CHECK_TIMEOUT = 5


async def _http_online() -> bool:
    import urllib.request

    try:
        req = urllib.request.Request(_CHECK_URL, method="HEAD")
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=_CHECK_TIMEOUT)
        return resp.status == 204
    except Exception:
        return False


async def _check_online(page: ft.Page) -> bool:
    connectivity = getattr(page, "connectivity", None)
    if connectivity is not None:
        try:
            if ft.ConnectivityType.NONE not in await connectivity.get_connectivity():
                return True
        except Exception as exc:
            LOG.info("connectivity service query failed: %s", exc)
    return await _http_online()


def _apply(online: bool) -> None:
    offline = not online
    if state.offline == offline:
        return
    state.offline = offline
    LOG.info("connectivity: %s", "online" if online else "offline")


async def recheck_connectivity(page: ft.Page) -> bool:
    online = await _check_online(page)
    _apply(online)
    return online


async def start_connectivity_monitor(page: ft.Page) -> None:
    """Run for the app lifetime (page.run_task). Cancel-aware via anyio."""
    connectivity = getattr(page, "connectivity", None)
    if connectivity is not None:

        async def _confirm_offline() -> None:
            _apply(await _check_online(page))

        def _on_change(e: ft.ConnectivityChangeEvent) -> None:
            if ft.ConnectivityType.NONE in e.connectivity:
                page.run_task(_confirm_offline)
            else:
                _apply(True)

        connectivity.on_change = _on_change

    while True:
        try:
            _apply(await _check_online(page))
        except Exception as exc:
            LOG.info("connectivity check error: %s", exc)
        await anyio.sleep(_CHECK_INTERVAL)


__all__ = ["recheck_connectivity", "start_connectivity_monitor"]
