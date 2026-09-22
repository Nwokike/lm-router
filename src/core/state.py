"""Global observable application state (voicelm pattern: scalars as class
defaults, containers assigned in __init__ so each instance owns them)."""

import flet as ft


@ft.observable
class AppState:
    selected_tab: int = 0
    active_view: str = "chat"
    theme_mode: str = "system"
    offline: bool = False
    onboarding_done: bool = False
    terms_accepted: bool = False
    settings_version: int = 0  # bumped on settings save (re-renders lists)

    # gateway (engine service)
    gateway_running: bool = False
    gateway_port: int = 8082
    gateway_version: str = ""
    gateway_uptime: int = 0
    gateway_source: str = ""  # e.g. "fetched 1.0.0" / "bundled 1.0.0"

    # catalog + chat
    model: str = ""
    busy: bool = False
    ad_can_request: bool = False
    sent_count: int = 0
    update_info: dict | None = None
    log_version: int = 0  # bumped when the log ring changes (re-renders Server screen)

    def __init__(self) -> None:
        self.gateway_base_url = f"http://127.0.0.1:{8082}/v1"
        self.models: list[dict] = []
        self.messages: list[dict] = []
        self.conversations: list[dict] = []
        self.active_conversation: str = ""
        self.mcp_tools: list[str] = []
        self.mcp_test_results: dict = {}

    @property
    def active_provider(self) -> str:
        return ""  # replaced at runtime by settings lookup in controllers


state = AppState()
AppStateCtx = ft.create_context(state)
