"""AdMob service: banner + interstitial with UMP consent (Sherlock port).

InterstitialAd and ConsentManager are ft.Service objects that self-register
under an active page context; we construct them inside page.run_task
coroutines (Sherlock's verified rule). Flet drops unreferenced services after
every event, so in-flight ads are kept in page.services until spent. Every
gate and failure is logged.
"""

import asyncio
import contextlib

import flet as ft

from core import constants
from core.logging import LOG

try:
    import flet_ads as fta

    _HAS_ADS = True
except ImportError:
    _HAS_ADS = False


class AdService:
    """Manages Mob interstitial ads and UMP consent."""

    @property
    def interstitial_id(self) -> str:
        # Production unit; CI hard-fails if a Google test ID ever lands here.
        return constants.AD_INTERSTITIAL_UNIT_ID_ANDROID

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.interstitial = None
        self._shown_interstitial = None
        self._on_close_cb = None
        self._can_request_ads: bool = True
        self._consent_manager = None
        self._privacy_options_required: bool | None = None
        self._is_shutting_down: bool = False

    def _is_mobile(self) -> bool:
        try:
            return self.page.platform.is_mobile()
        except Exception as exc:
            LOG.debug("platform query failed: %s", exc)
            return False

    async def gather_consent(self) -> None:
        """Run the UMP consent flow; only shows UI in regulated regions."""
        if not _HAS_ADS:
            LOG.info("ads: flet_ads unavailable, ads disabled this build")
            self._can_request_ads = False
            return
        if not self._is_mobile():
            LOG.info("ads: non-mobile platform, consent flow skipped")
            self._can_request_ads = True
            return
        try:
            self._consent_manager = fta.ConsentManager()
            if self._consent_manager not in self.page.services:
                self.page.services.append(self._consent_manager)
            await self._consent_manager.request_consent_info_update()
            await self._consent_manager.load_and_show_consent_form_if_required()
            self._can_request_ads = await self._consent_manager.can_request_ads()
            try:
                status = await self._consent_manager.get_privacy_options_requirement_status()
                self._privacy_options_required = (
                    status == fta.PrivacyOptionsRequirementStatus.REQUIRED
                )
                LOG.info(
                    "ads: consent status=%s can_request_ads=%s",
                    status,
                    self._can_request_ads,
                )
            except Exception as exc:
                LOG.warning("ads: privacy-requirement probe failed: %s", exc)
            if not self._can_request_ads:
                LOG.warning("ads: UMP says ads cannot be requested this session")
        except Exception as exc:
            LOG.warning("ads: consent flow failed, defaulting to allow: %s", exc)
            self._can_request_ads = True

    async def show_privacy_options(self) -> str:
        if not self._consent_manager:
            return "no_manager"
        try:
            status = await self._consent_manager.get_privacy_options_requirement_status()
        except Exception as exc:
            LOG.warning("ads: privacy options status failed: %s", exc)
            return f"error:{exc}"
        if status != fta.PrivacyOptionsRequirementStatus.REQUIRED:
            return "not_required"
        try:
            await self._consent_manager.show_privacy_options_form()
            self._can_request_ads = await self._consent_manager.can_request_ads()
            return "form_shown"
        except Exception as exc:
            LOG.warning("ads: privacy options form failed: %s", exc)
            return f"error:{exc}"

    async def preload_interstitial(self, on_close=None) -> None:
        self._on_close_cb = on_close
        if self._is_shutting_down:
            return
        if not _HAS_ADS or not self._is_mobile():
            return
        if not self._can_request_ads:
            return
        try:
            ad = fta.InterstitialAd(
                unit_id=self.interstitial_id,
                on_load=lambda e: LOG.info("ads: interstitial loaded"),
                on_error=lambda e: LOG.warning(
                    "ads: interstitial load error: %s",
                    getattr(e, "data", e),
                ),
                on_close=self._handle_close,
            )
        except Exception as exc:
            LOG.warning("ads: interstitial construction failed: %s", exc)
            return
        self.interstitial = ad
        if ad not in self.page.services:
            self.page.services.append(ad)
        LOG.info("ads: interstitial preloaded")

    async def _handle_close(self, e) -> None:
        if self._shown_interstitial is not None:
            _release_service(self.page.services, self._shown_interstitial)
            self._shown_interstitial = None
        if self._on_close_cb is not None:
            if asyncio.iscoroutinefunction(self._on_close_cb):
                await self._on_close_cb()
            else:
                self._on_close_cb()

    async def show_interstitial(self) -> bool:
        """Show an interstitial: preloaded when ready, else a fresh one."""
        if not _HAS_ADS or not self._is_mobile() or not self._can_request_ads:
            return False
        if self.interstitial is not None:
            ad = self.interstitial
            self.interstitial = None
            self._shown_interstitial = ad
            try:
                await ad.show()
                LOG.info("ads: interstitial shown (preloaded)")
                return True
            except Exception as exc:
                LOG.warning("ads: preloaded show failed: %s", exc)
                self._shown_interstitial = None
                _release_service(self.page.services, ad)
                return False
            finally:
                if not self._is_shutting_down:
                    await self.preload_interstitial(on_close=self._on_close_cb)
        try:

            async def _show(e) -> None:
                await e.control.show()
                LOG.info("ads: interstitial shown (fresh)")

            ad = fta.InterstitialAd(
                unit_id=self.interstitial_id,
                on_load=lambda e: self.page.run_task(_show, e),
                on_error=lambda e: LOG.warning(
                    "ads: interstitial load error: %s",
                    getattr(e, "data", e),
                ),
                on_close=self._handle_close,
            )
        except Exception as exc:
            LOG.warning("ads: fresh interstitial construction failed: %s", exc)
            return False
        self._shown_interstitial = ad
        if ad not in self.page.services:
            self.page.services.append(ad)
        return True

    async def close(self) -> None:
        self._is_shutting_down = True
        for ad in (self.interstitial, self._shown_interstitial):
            if ad is not None:
                _release_service(self.page.services, ad)
        self.interstitial = None
        self._shown_interstitial = None


def _release_service(services: list, item) -> None:
    with contextlib.suppress(ValueError):
        services.remove(item)
