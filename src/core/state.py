"""Global observable application state (voicelm pattern: scalars as class
defaults, containers assigned in __init__ so each instance owns them)."""

import flet as ft

__all__ = ["AppState", "AppStateCtx", "state"]


@ft.observable
class AppState:
    selected_tab: int = 0
    # Last real NavigationBar tab (0-2). History is gone; this only tracks
    # the last real tab so the bar keeps highlighting where the user came from.
    last_nav_tab: int = 0
    theme_mode: str = "system"
    offline: bool = False
    onboarding_done: bool = False
    terms_accepted: bool = False
    settings_version: int = 0  # bumped on settings save (re-renders lists)

    # gateway (engine service)
    gateway_running: bool = False
    # True while a start is in flight, so the button can show progress
    # and refuse a double press instead of racing itself.
    gateway_starting: bool = False
    gateway_port: int = 8082
    gateway_version: str = ""
    gateway_uptime: int = 0
    gateway_source: str = ""  # e.g. "fetched 1.0.0" / "bundled 1.0.0"
    gateway_lan_ip: str = ""
    gateway_lan_url: str = ""

    # sharing (public tunnel in front of the gateway).
    share_starting: bool = False
    share_error: str = ""

    # model selection (shared: test screen + server screen)
    model: str = ""

    update_info: dict | None = None
    log_version: int = 0  # bumped when the log ring changes (re-renders Server screen)

    # Global error/notice banner (dismissed by the user; never swallowed)
    notice: str = ""

    # Model test bench (console parity): sweep state + per-row verdicts.
    model_testing: frozenset = frozenset()
    retesting: bool = False
    retest_progress: tuple = (0, 0)

    def __init__(self) -> None:
        self.gateway_base_url = f"http://127.0.0.1:{8082}/v1"
        self.models: list[dict] = []
        self.model_test_results: dict = {}
        self.model_test_results: dict = {}
        # Per-model rate-limit hints from GET /account-limits, keyed by model id.
        self.rate_hints: dict = {}
        # Aggregate gateway counters from GET /status (no model ids upstream).
        self.gateway_counts: dict = {}
        # Public tunnel URL while sharing; empty when not sharing.
        self.share_url: str = ""


state = AppState()
AppStateCtx = ft.create_context(state)
