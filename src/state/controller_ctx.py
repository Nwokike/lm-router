"""Controller method surface injected into the component tree.

Pattern from his apps: every screen reads ControllerMethodsCtx and calls
these; AppController.init fills the dataclass with real implementations.
No-op defaults let screens render (and tests import) before wiring exists.
"""

from collections.abc import Callable
from dataclasses import dataclass

import flet as ft


def _noop(*args: object, **kwargs: object) -> None:
    return None


@dataclass
class ControllerMethods:
    # shell
    set_tab: Callable[[int], None] = _noop
    set_theme: Callable[[str], None] = _noop
    dismiss_update: Callable[[], None] = _noop
    dismiss_notice: Callable[[], None] = _noop
    # chat
    send_message: Callable[[str], None] = _noop
    regenerate_last: Callable[[], None] = _noop
    edit_last_user: Callable[[str], None] = _noop
    stop_generation: Callable[[], None] = _noop
    set_model: Callable[[str], None] = _noop
    toggle_search_tool: Callable[[], None] = _noop
    open_mcp_tools: Callable[[], None] = _noop
    new_conversation: Callable[[], None] = _noop
    open_conversation: Callable[[str], None] = _noop
    delete_conversation: Callable[[str], None] = _noop
    delete_message_at: Callable[[int], None] = _noop
    copy_text: Callable[[str], None] = _noop
    copy_logs: Callable[[], None] = _noop
    export_conversation: Callable[[str], None] = _noop
    # gateway
    start_gateway: Callable[[], None] = _noop
    stop_gateway: Callable[[], None] = _noop
    refresh_models: Callable[[], None] = _noop
    open_gateway_console: Callable[[], None] = _noop
    open_log_terminal: Callable[[], None] = _noop
    # share
    start_share: Callable[[], None] = _noop
    stop_share: Callable[[], None] = _noop
    regenerate_share_key: Callable[[], None] = _noop
    # model test bench
    test_model: Callable[[str], None] = _noop
    retest_models: Callable[[bool], None] = _noop
    stop_retest: Callable[[], None] = _noop
    # settings
    save_settings: Callable[[dict], None] = _noop
    add_provider: Callable[[dict], None] = _noop
    remove_provider: Callable[[str], None] = _noop
    select_provider: Callable[[str], None] = _noop
    add_mcp_server: Callable[[dict], None] = _noop
    remove_mcp_server: Callable[[str], None] = _noop
    toggle_mcp_server: Callable[[str], None] = _noop
    toggle_mcp_tool: Callable[[str, str], None] = _noop
    test_mcp_server: Callable[[str, object], None] = _noop  # (server_id, done_callback)
    clear_history: Callable[[], None] = _noop
    check_update: Callable[[], None] = _noop
    open_update_dialog: Callable[[], None] = _noop
    open_about: Callable[[], None] = _noop
    quit_app: Callable[[], None] = _noop
    finish_onboarding: Callable[[], None] = _noop
    open_url: Callable[[str], None] = _noop


ControllerMethodsCtx = ft.create_context(ControllerMethods())
