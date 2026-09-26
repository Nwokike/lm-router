"""Global observable application state (voicelm pattern: scalars as class
defaults, containers assigned in __init__ so each instance owns them)."""

import flet as ft

__all__ = ["AppState", "AppStateCtx", "state"]


@ft.observable
class AppState:
    selected_tab: int = 0
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

    # sharing (public tunnel in front of the gateway). The claim retries for
    # up to 30s, so the flag is an in-flight guard: a second click would
    # start a SECOND proxy instead of joining the first.
    share_starting: bool = False
    # Rendered inline in the share card; never a dead attribute on the
    # controller (a failure the owner cannot see is a failure that did not
    # happen as far as he is concerned).
    share_error: str = ""

    # catalog + chat
    model: str = ""
    busy: bool = False
    search_enabled: bool = True
    context_used_tokens: int = 0
    sent_count: int = 0
    update_info: dict | None = None
    log_version: int = 0  # bumped when the log ring changes (re-renders Server screen)

    # Global error/notice banner (dismissed by the user; never swallowed)
    notice: str = ""

    # Settings: server-ids with an in-flight "Test" (spinner + re-click guard)
    mcp_testing: frozenset = frozenset()

    def __init__(self) -> None:
        self.gateway_base_url = f"http://127.0.0.1:{8082}/v1"
        self.models: list[dict] = []
        self.messages: list[dict] = []
        self.conversations: list[dict] = []
        self.active_conversation: str = ""
        self.mcp_tools: list[str] = []
        self.mcp_test_results: dict = {}
        # Per-model rate-limit hints from GET /account-limits, keyed by model
        # id. Drives the "this model is capped" affordances in the UI.
        self.rate_hints: dict = {}
        # Aggregate gateway counters from GET /status (no model ids upstream).
        self.gateway_counts: dict = {}
        # Public tunnel URL while sharing; empty when not sharing.
        self.share_url: str = ""

    @property
    def active_provider(self) -> str:
        return ""  # replaced at runtime by settings lookup in controllers


state = AppState()
AppStateCtx = ft.create_context(state)
