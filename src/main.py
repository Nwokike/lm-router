"""LM Router entry point."""

import os
import time
from typing import Any

import flet as ft

from app_shell import AppShell
from components.connectivity_monitor import start_connectivity_monitor
from components.update_dialog import build_update_dialog
from core import constants, theme
from core.logging import LOG
from core.settings import AppSettings, MCPServerConfig, ProviderConfig
from core.state import state
from services import history
from services.ad_service import AdService
from services.agent import AgentService
from services.engine import EngineService
from services.http import HttpService
from services.mcp import MCPHub, enabled_params
from services.search import build_search_tool
from services.update_service import UpdateService
from state.controller_ctx import ControllerMethods, ControllerMethodsCtx
from state.service_ctx import ServiceCtx, Services

THEME_MODES = {
    "system": ft.ThemeMode.SYSTEM,
    "light": ft.ThemeMode.LIGHT,
    "dark": ft.ThemeMode.DARK,
}


class AppController:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.settings = AppSettings.load()
        self.services = Services()
        self.methods = ControllerMethods()
        self._url_launcher: object | None = None
        self.ads: AdService | None = None

    def init(self) -> None:
        page = self.page
        settings = self.settings

        state.theme_mode = settings.theme
        state.gateway_port = settings.gateway_port
        state.onboarding_done = settings.onboarding_done
        state.terms_accepted = settings.terms_accepted
        page.theme = theme.get_light_theme()
        page.dark_theme = theme.get_dark_theme()
        page.theme_mode = THEME_MODES.get(settings.theme, ft.ThemeMode.SYSTEM)
        page.fonts = theme.FONTS

        # Keep-alive: closing the window hides it to the taskbar so the
        # gateway thread keeps running (locked v1 decision).
        try:
            if page.platform.is_desktop():
                page.window.prevent_close = True
                page.window.on_event = self._on_window_event
        except Exception as exc:
            LOG.info("window close hook unavailable: %s", exc)

        # services
        http = HttpService()
        engine = EngineService(settings)
        hub = MCPHub(settings)
        agent = AgentService(settings, extra_tools=self._extra_tools)
        self.services.settings = settings
        self.services.http = http
        self.services.engine = engine
        self.services.mcp = hub
        self.services.agent = agent
        self._search_tool = build_search_tool(http)
        agent.start()  # anyio portal thread: hosts kani turns and httpx calls
        try:
            self._url_launcher = ft.UrlLauncher()
            page.services.append(self._url_launcher)
        except Exception as exc:
            LOG.info("url launcher unavailable: %s", exc)
        try:
            page.services.append(ft.HapticFeedback())
        except Exception as exc:
            LOG.info("haptics unavailable: %s", exc)

        # conversation catalog on boot, fresh id for new chats
        state.conversations = history.list_conversations()
        if not state.active_conversation:
            history.start_conversation()

        # connectivity: OS events + HTTP confirm, banner rendered by the shell
        try:
            page.services.append(ft.Connectivity())
        except Exception as exc:
            LOG.info("connectivity service unavailable: %s", exc)
        page.run_task(start_connectivity_monitor, page)

        # ads: UMP consent then preload (gates live inside AdService)
        self.ads = AdService(page)
        page.run_task(self._boot_ads)

        # connect any enabled MCP servers once, up front (long-lived hold)
        if settings.mcp_servers:
            self._reapply_mcp(blocking=True)

        # controllers wired for this step (later steps extend the same object)
        m = self.methods
        m.set_tab = self._set_tab
        m.set_theme = self._set_theme
        m.start_gateway = self._start_gateway
        m.stop_gateway = self._stop_gateway
        m.refresh_models = self._refresh_models
        m.open_gateway_console = self._open_console
        m.quit_app = self._quit_app
        m.send_message = self._send_message
        m.stop_generation = self._stop_generation
        m.set_model = self._set_model
        m.new_conversation = self._new_conversation
        m.finish_onboarding = self._finish_onboarding
        m.open_url = self._open_url
        m.add_mcp_server = self._add_mcp_server
        m.remove_mcp_server = self._remove_mcp_server
        m.toggle_mcp_server = self._toggle_mcp_server
        m.test_mcp_server = self._test_mcp_server
        m.save_settings = self._save_settings
        m.add_provider = self._add_provider
        m.remove_provider = self._remove_provider
        m.select_provider = self._select_provider
        m.clear_history = self._clear_history
        m.open_conversation = self._open_conversation
        m.delete_conversation = self._delete_conversation
        m.check_update = lambda: self.page.run_task(self._check_update)
        m.open_update_dialog = self._open_update_dialog
        m.dismiss_update = self._dismiss_update

        import core.logging as applog

        applog.on_record = self._bump_logs
        LOG.info("LM Router %s starting", constants.APP_VERSION)

        if settings.gateway_autostart:
            page.run_thread(self._start_gateway_quiet)
        if settings.update_check:
            page.run_task(self._check_update, True)

    # controller implementations

    def _bump_logs(self) -> None:
        state.log_version += 1

    def _on_window_event(self, event: object) -> None:
        name = getattr(event, "data", "") or ""
        if "CLOSE" in str(name).upper():
            self.page.window.visible = False

    def _set_tab(self, index: int) -> None:
        state.selected_tab = index

    def _set_theme(self, mode: str) -> None:
        state.theme_mode = mode
        self.page.theme_mode = THEME_MODES.get(mode, ft.ThemeMode.SYSTEM)
        self.settings.theme = mode  # type: ignore[assignment]
        self.settings.save()

    def _start_gateway_quiet(self) -> None:
        try:
            self.services.engine.start()
        except Exception as exc:
            LOG.error("gateway autostart failed: %s", exc)
            return
        self._fetch_models(refresh=False)

    def _start_gateway(self) -> None:
        self.page.run_thread(self._start_gateway_quiet)

    def _stop_gateway(self) -> None:
        self.page.run_thread(self.services.engine.stop)

    def _refresh_models(self) -> None:
        # refresh=true makes the engine force a fresh upstream probe server-side
        self.page.run_thread(lambda: self._fetch_models(refresh=True))

    def _fetch_models(self, refresh: bool) -> None:
        agent = self.services.agent
        if agent.portal is None:
            LOG.warning("cannot fetch models: agent portal down")
            return

        async def fetch() -> list:
            suffix = "?refresh=true" if refresh else ""
            resp = await self.services.http.get(f"{state.gateway_base_url}/v1/models{suffix}")
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            return list(payload.get("data", []))

        try:
            data = agent.call(fetch)
        except Exception as exc:
            LOG.warning("model catalog fetch failed: %s", exc)
            return
        self._apply_models(data)

    def _apply_models(self, data: list) -> None:
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        state.models = data
        if state.model not in ids:
            preferred = (
                self.settings.last_model
                if self.settings.last_model in ids
                else (ids[0] if ids else "")
            )
            if preferred:
                state.model = preferred
                if self.settings.last_model != preferred:
                    self.settings.last_model = preferred
                    self.settings.save()
        LOG.info("model catalog: %d models", len(ids))

    def _set_model(self, model: str) -> None:
        if not model or model == state.model:
            return
        state.model = model
        self.settings.last_model = model
        self.settings.save()
        LOG.info("model selected: %s", model)

    def _new_conversation(self) -> None:
        state.messages = []
        self.services.agent.reset_conversation()
        history.start_conversation()

    def _stop_generation(self) -> None:
        self.services.agent.stop_turn()

    def _send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or state.busy:
            return
        if not state.gateway_running:
            state.messages.append(
                {"role": "error", "content": "Gateway is not running. Start it on the Server tab."}
            )
            return
        agent = self.services.agent
        kani = agent.ensure_kani(state.model, self.settings.system_prompt)
        if kani is None:
            state.messages.append({"role": "error", "content": "No model selected yet."})
            return

        state.messages.append({"role": "user", "content": text})
        state.messages.append({"role": "assistant", "content": ""})
        index = len(state.messages) - 1
        state.busy = True
        buffer = {"text": "", "last": 0.0}

        def flush() -> None:
            state.messages[index] = {"role": "assistant", "content": buffer["text"]}

        def on_delta(chunk: str) -> None:
            buffer["text"] += chunk
            now = time.monotonic()
            if now - buffer["last"] >= 0.08:
                buffer["last"] = now
                flush()

        def on_tool(name: str, content: str, is_error: bool) -> None:
            state.messages.append(
                {
                    "role": "tool",
                    "name": name,
                    "content": content[:6000],
                    "is_error": is_error,
                }
            )
            LOG.info("tool result: %s%s", name, " (error)" if is_error else "")

        def on_done(message: Any, usage: dict | None) -> None:
            final = buffer["text"] or str(getattr(message, "text", "") or "")
            entry: dict = {"role": "assistant", "content": final}
            if usage:
                entry["usage"] = usage
            state.messages[index] = entry
            state.busy = False
            state.sent_count += 1
            history.save_conversation(self.services.agent)
            self._maybe_show_interstitial()
            LOG.info("turn done: %s tokens", (usage or {}).get("total_tokens", "?"))

        def on_error(kind: str, content: str) -> None:
            if kind == "stopped":
                flush()
                state.messages[index] = {**state.messages[index], "stopped": True}
            elif buffer["text"]:
                flush()
                state.messages.append({"role": "error", "content": content})
                LOG.warning("turn error (%s): %s", kind, content)
            else:
                state.messages[index] = {"role": "error", "content": content}
                LOG.warning("turn error (%s): %s", kind, content)
            state.busy = False

        started = agent.start_turn(text, on_delta, on_done, on_error, on_tool=on_tool)
        if not started:
            tail = state.messages[-1]
            if tail.get("role") == "assistant" and tail.get("content") == "":
                state.messages.pop()
            state.busy = False

    def _open_console(self) -> None:
        url = f"http://127.0.0.1:{self.services.engine.port}/"
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open console: url launcher missing")
            return
        try:
            self.page.run_thread(lambda: launcher.launch_url(url))
        except Exception as exc:
            LOG.warning("open console failed: %s", exc)

    def _extra_tools(self) -> tuple[list, object]:
        """(tools, generation) for AgentService; rebuilds kani on change."""
        tools: list = []
        if self.settings.search_enabled and self._search_tool is not None:
            tools.append(self._search_tool)
        tools.extend(self.services.mcp.tools)
        return tools, f"{self.services.mcp.generation}:{self.settings.search_enabled}"

    def _reapply_mcp(self, blocking: bool = False) -> None:
        hub = self.services.mcp
        agent = self.services.agent
        params = enabled_params(self.settings)

        def work() -> None:
            async def apply() -> None:
                await hub.apply(params)

            try:
                agent.call(apply)
            except Exception as exc:
                LOG.warning("mcp apply failed: %s", exc)
                return
            state.mcp_tools = list(hub.names)
            LOG.info("mcp tools active: %d", len(hub.names))

        if blocking:
            work()
        else:
            self.page.run_thread(work)

    def _add_mcp_server(self, data: dict) -> None:
        try:
            self.settings.mcp_servers.append(MCPServerConfig(**data))
        except Exception as exc:
            LOG.warning("invalid mcp server: %s", exc)
            return
        self.settings.save()
        self._reapply_mcp()

    def _remove_mcp_server(self, server_id: str) -> None:
        self.settings.mcp_servers = [s for s in self.settings.mcp_servers if s.id != server_id]
        self.settings.save()
        self._reapply_mcp()

    def _toggle_mcp_server(self, server_id: str) -> None:
        for server in self.settings.mcp_servers:
            if server.id == server_id:
                server.enabled = not server.enabled
        self.settings.save()
        self._reapply_mcp()

    def _test_mcp_server(self, server_id: str, done_cb) -> None:
        hub = self.services.mcp
        agent = self.services.agent
        target = next((s for s in self.settings.mcp_servers if s.id == server_id), None)
        if target is None:
            done_cb(("err", "Server not found."))
            return

        def work() -> None:
            async def probe() -> list:
                return await hub.test(target)

            try:
                names = agent.call(probe)
            except Exception as exc:
                done_cb(("err", str(exc)[:300]))
                return
            done_cb(("ok", names))

        self.page.run_thread(work)

    def _save_settings(self, data: dict) -> None:
        allowed = {
            "theme",
            "gateway_port",
            "gateway_autostart",
            "system_prompt",
            "search_enabled",
            "interstitial_every",
            "update_check",
            "max_context_tokens",
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return
        previous = {k: getattr(self.settings, k) for k in updates}
        try:
            for key, value in updates.items():
                setattr(self.settings, key, value)
        except Exception as exc:
            LOG.warning("settings value rejected (%s); reverting", exc)
            for key, value in previous.items():
                try:
                    setattr(self.settings, key, value)
                except Exception as revert_exc:
                    LOG.debug("revert %s failed: %s", key, revert_exc)
            return
        self.settings.save()
        state.settings_version += 1

    def _add_provider(self, data: dict) -> None:
        try:
            provider = ProviderConfig(**data)
        except Exception as exc:
            LOG.warning("invalid provider: %s", exc)
            return
        self.settings.providers.append(provider)
        self.settings.save()
        state.settings_version += 1

    def _remove_provider(self, provider_id: str) -> None:
        self.settings.providers = [p for p in self.settings.providers if p.id != provider_id]
        if self.settings.active_provider_id == provider_id:
            self.settings.active_provider_id = ""
        self.settings.save()
        state.settings_version += 1

    def _select_provider(self, provider_id: str) -> None:
        if not any(p.id == provider_id for p in self.settings.providers):
            return
        self.settings.active_provider_id = provider_id
        self.settings.save()
        state.settings_version += 1
        LOG.info("active provider: %s", provider_id)

    def _clear_history(self) -> None:
        history.clear_all()
        self._new_conversation()

    def _open_conversation(self, conversation_id: str) -> None:
        agent = self.services.agent
        if agent.ensure_kani(state.model, self.settings.system_prompt) is None:
            LOG.warning("cannot open conversation: no model yet")
            return
        if history.load_conversation(agent, conversation_id):
            self._set_tab(0)

    def _delete_conversation(self, conversation_id: str) -> None:
        history.delete_conversation(conversation_id)

    async def _check_update(self, silent: bool = False) -> None:
        data = await UpdateService.check_for_updates()
        if not data:
            if not silent:
                LOG.info("no update available")
            return
        state.update_info = data
        if not silent:
            self._open_update_dialog()

    def _open_update_dialog(self) -> None:
        if not state.update_info:
            LOG.info("no update to show")
            return
        dialog = build_update_dialog(self.page, state.update_info, self._url_launcher)
        self.page.show_dialog(dialog)

    def _dismiss_update(self) -> None:
        try:
            self.page.pop_dialog()
        except Exception as exc:
            LOG.debug("dismiss update: %s", exc)
        state.update_info = None

    async def _boot_ads(self) -> None:
        if self.ads is None:
            return
        await self.ads.gather_consent()
        await self.ads.preload_interstitial()

    def _maybe_show_interstitial(self) -> None:
        every = self.settings.interstitial_every
        ads = self.ads
        if ads is None or every < 1:
            return
        if state.sent_count and state.sent_count % every == 0:
            self.page.run_task(ads.show_interstitial)

    def _finish_onboarding(self) -> None:
        self.settings.onboarding_done = True
        self.settings.terms_accepted = True
        self.settings.save()
        state.onboarding_done = True
        state.terms_accepted = True
        LOG.info("onboarding complete")

    def _open_url(self, url: str) -> None:
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open %s: url launcher missing", url)
            return
        try:
            self.page.run_thread(lambda u=url: launcher.launch_url(u))
        except Exception as exc:
            LOG.warning("open url failed: %s", exc)

    def _quit_app(self) -> None:
        try:
            self.services.agent.stop()
        except Exception as exc:
            LOG.warning("agent shutdown: %s", exc)
        try:
            self.services.engine.stop()
        except Exception as exc:
            LOG.warning("gateway shutdown: %s", exc)
        try:
            self.page.window.prevent_close = False
            self.page.window.destroy()
        except Exception as exc:
            LOG.warning("quit failed: %s", exc)


def main(page: ft.Page) -> None:
    controller = AppController(page)
    controller.init()
    services = controller.services
    methods = controller.methods
    page.render(
        lambda: ServiceCtx(services, lambda: ControllerMethodsCtx(methods, lambda: AppShell()))
    )


if __name__ == "__main__":
    ft.run(main, assets_dir=os.environ.get("FLET_ASSETS_DIR") or "src/assets")
