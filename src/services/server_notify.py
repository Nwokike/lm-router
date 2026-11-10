"""Android foreground service + wake lock for the LM Router gateway.

Keeps the Android process alive when the app is minimised while the gateway
is running. A sticky foreground notification appears with an Exit action.
Tapping Exit performs the full app exit: stop the FGS, then kill the process
(page-independent, so it works from the shade even after the UI session has
expired) — CollabShell's session_notify.exit_app pattern, verbatim.

Ported from CollabShell's services/session_notify.py (which itself came from
proot-distro-flet) — same flet-notifications pattern, same guard style.
"""

from __future__ import annotations

import asyncio
import logging
import os

import flet as ft

LOG = logging.getLogger("lmrouter")

_FGS_ID = 1  # nonzero: the plugin rejects id=0
# Our own app-icon silhouette, bundled via [tool.flet.android] res_dir
# (android_res/drawable-*/ic_stat_lmrouter.png) — same pattern as CollabShell
# and proot-distro. Never the plugin's flat default.
_FGS_ICON = "ic_stat_lmrouter"


class ServerNotificationService:
    """Owns the flet_notifications.Notifications service and ft.Wakelock.

    Call mount(page) once during AppController.init(), then start(port) /
    stop() as the gateway comes up and goes down.
    """

    def __init__(self, *, on_stop=None) -> None:
        self._on_stop = on_stop
        self._page: ft.Page | None = None
        self._ftn = None  # the flet_notifications module (None if not installed)
        self._notifications = None  # ftn.Notifications instance
        self._wakelock: ft.Wakelock | None = None
        self._fgs_running = False

    # ── mount ──────────────────────────────────────────────────────────────

    def mount(self, page: ft.Page) -> None:
        """Create and register both services with the page (once per session)."""
        self._page = page

        # Wake lock — always available, no extra package needed.
        self._wakelock = ft.Wakelock()
        if self._wakelock not in page.services:
            page.services.append(self._wakelock)

        # Notifications — soft import so the app still runs on desktop without
        # the package installed (the flet-notifications wheel is Android/mobile).
        try:
            import flet_notifications as ftn
        except ImportError:
            LOG.info("flet_notifications not installed; FGS disabled")
            return
        self._ftn = ftn
        self._notifications = ftn.Notifications(android_default_icon=_FGS_ICON)
        self._notifications.on_action = self._handle_action
        self._notifications.on_tap = self._handle_tap
        if self._notifications not in page.services:
            page.services.append(self._notifications)
        LOG.info("notification service mounted")

    # ── permissions ────────────────────────────────────────────────────────

    async def request_permission(self) -> None:
        """Request POST_NOTIFICATIONS at boot — never fatal if denied."""
        if self._notifications is None:
            return
        try:
            await self._notifications.request_permissions()
        except Exception as exc:
            LOG.debug("notification permission request failed: %s", exc)

    # ── foreground service lifecycle ───────────────────────────────────────

    def _is_android(self) -> bool:
        try:
            page = self._page
            return (
                page is not None
                and not getattr(page, "web", False)
                and page.platform == ft.PagePlatform.ANDROID
            )
        except Exception:
            return False

    async def start(self, port: int) -> None:
        """Start the FGS notification + wake lock when the gateway comes up."""
        await self.wakelock_set(True)
        if self._notifications is None or not self._is_android():
            return
        if self._fgs_running:
            return
        ftn = self._ftn
        try:
            await self.request_permission()
            await self._notifications.start_foreground_service(
                id=_FGS_ID,
                title="LM Router",
                body=f"Gateway running \u00b7 port {port}",
                payload="gateway:running",
                start_type=ftn.AndroidServiceStartType.STICKY,
                service_types=[ftn.AndroidForegroundServiceType.SPECIAL_USE],
                actions=[ftn.NotificationAction(id="exit", title="Exit")],
            )
            self._fgs_running = True
            LOG.info("FGS started (port %d)", port)
        except Exception as exc:
            LOG.warning("FGS start failed: %s", exc)

    async def stop(self) -> None:
        """Remove the FGS notification + release the wake lock.

        One call covers every shutdown path (gateway stop, in-app Quit,
        notification Exit) so no wake lock leaks when the process outlives
        the UI session.
        """
        await self.wakelock_set(False)
        if self._notifications is None or not self._fgs_running:
            return
        try:
            await self._notifications.stop_foreground_service()
            LOG.info("FGS stopped")
        except Exception as exc:
            LOG.debug("FGS stop failed: %s", exc)
        finally:
            self._fgs_running = False

    # ── wake lock ──────────────────────────────────────────────────────────

    async def wakelock_set(self, on: bool) -> None:
        """Enable or disable the screen wake lock — never crashes the app."""
        if self._wakelock is None:
            return
        try:
            if on:
                await self._wakelock.enable()
            else:
                await self._wakelock.disable()
        except Exception as exc:
            LOG.debug("wakelock set(%s) failed: %s", on, exc)

    # ── notification callbacks ─────────────────────────────────────────────

    def _handle_action(self, e) -> None:
        """User pressed an action button on the notification."""
        if getattr(e, "action_id", "") != "exit":
            return
        # Exit must work from the shade even when the UI session is dead, so
        # it runs page-independent: FGS stop, then process kill. No Flet
        # loop involvement at all.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None:
                loop.create_task(self.exit_app())
            else:
                asyncio.run(self.exit_app())
        except Exception as exc:
            LOG.debug("notification exit dispatch failed: %s", exc)

    async def exit_app(self) -> None:
        """The notification's Exit button: stop FGS, then kill the process.

        CollabShell's session_notify.exit_app verbatim: FGS stop, jnius
        Process.killProcess(myPid()), os._exit(0) as the fallback when jnius
        is unavailable (desktop/tests). The gateway dies with the process —
        that is what "Exit closes it" means.
        """
        try:
            await self.stop()
        except Exception as exc:
            LOG.debug("exit: FGS stop failed: %s", exc)
        try:
            from jnius import autoclass  # type: ignore

            proc = autoclass("android.os.Process")
            proc.killProcess(proc.myPid())
            return
        except Exception as exc:
            LOG.debug("jnius process kill unavailable (%s); using os._exit", exc)
        os._exit(0)

    def _handle_tap(self, e) -> None:
        """User tapped the notification body — no action needed (just brings app forward)."""
