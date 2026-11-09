"""Android foreground service + wake lock for the LM Router gateway.

Keeps the Android process alive when the app is minimised while the gateway
is running. A sticky foreground notification appears with a Stop action.
Tapping Stop calls the on_stop callback which runs _stop_gateway().

Ported from CollabShell's services/session_notify.py (which itself came from
proot-distro-flet) — same flet-notifications 1.1.0 pattern, same guard style.
"""

from __future__ import annotations

import logging

import flet as ft

LOG = logging.getLogger("lmrouter")

_FGS_ID = 1  # nonzero: the plugin rejects id=0
_FGS_ICON = "flet_notifications_icon"  # default silhouette shipped by the plugin


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
        """Start the FGS notification when the gateway comes up."""
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
                actions=[ftn.NotificationAction(id="stop", title="Stop")],
            )
            self._fgs_running = True
            LOG.info("FGS started (port %d)", port)
        except Exception as exc:
            LOG.warning("FGS start failed: %s", exc)

    async def stop(self) -> None:
        """Remove the FGS notification when the gateway stops."""
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
        if getattr(e, "action_id", "") != "stop":
            return
        # Fire the stop-gateway callback on the Flet loop, same as a UI tap.
        if self._on_stop is not None and self._page is not None:
            try:
                self._page.run_task(self._async_stop_gateway)
            except Exception as exc:
                LOG.debug("notification stop action dispatch failed: %s", exc)

    async def _async_stop_gateway(self) -> None:
        """Thin async wrapper so run_task can call the sync _on_stop callback."""
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception as exc:
                LOG.debug("notification stop callback failed: %s", exc)

    def _handle_tap(self, e) -> None:
        """User tapped the notification body — no action needed (just brings app forward)."""
