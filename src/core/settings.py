"""Typed application settings with JSON persistence and encrypted keys.

Environment/dotenv sources are deliberately disabled via
settings_customise_sources (the pydantic-settings FASTMCP_ lesson): init
values are the only input, the file on disk is managed explicitly below, so
no ambient env var can silently override user choices.
"""

import contextlib
import json
import uuid
from typing import Literal

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import constants, storage
from .logging import LOG

# Warnings collected while parsing stored settings (dropped keys, malformed
# items). Popped by the app at boot and shown in the notice banner so silent
# data loss never happens (settings audit, invisible-error list).
_LOAD_WARNINGS: list[str] = []


def pop_load_warnings() -> list[str]:
    global _LOAD_WARNINGS
    out, _LOAD_WARNINGS = _LOAD_WARNINGS, []
    return out


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, max_length=64)
    base_url: AnyUrl
    api_key: SecretStr | None = None
    models: list[str] = []
    enabled: bool = True


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, max_length=64)
    transport: Literal["stdio", "sse", "streamable_http"] = "streamable_http"
    command: str | None = None
    args: list[str] = []
    url: AnyUrl | None = None
    headers: dict[str, str] = {}
    disabled_tools: list[str] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerConfig:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio servers need a command")
        if self.transport != "stdio" and not self.url:
            raise ValueError("remote servers need a url")
        return self


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", validate_assignment=True)

    settings_version: int = 1
    theme: Literal["system", "light", "dark"] = "system"
    onboarding_done: bool = False
    terms_accepted: bool = False
    system_prompt: str = (
        "You are LM Router, a helpful assistant served through the user's local gateway."
    )
    last_model: str = ""
    gateway_port: int = Field(default=constants.DEFAULT_GATEWAY_PORT, ge=1, le=65535)
    gateway_autostart: bool = True
    # Desktop: X hides the window and the gateway keeps serving. Off means
    # X closes the app and stops the gateway. Either way there is a
    # visible Quit, so the app can never look crashed.
    keep_running_when_closed: bool = True
    # Share: publish this device's gateway through a public tunnel.
    # Off by default; the gateway itself has no auth, so turning this on
    # with require_share_key=False is an open relay by choice.
    share_enabled: bool = False
    require_share_key: bool = False
    share_key: str = ""
    search_enabled: bool = True
    interstitial_every: int = Field(default=constants.INTERSTITIAL_EVERY, ge=1)
    providers: list[ProviderConfig] = []
    active_provider_id: str = ""
    mcp_servers: list[MCPServerConfig] = []
    max_context_tokens: int = Field(default=128_000, ge=1024)
    update_check: bool = True

    # ── Generation controls ────────────────────────────────────────────────
    # Passed through to kani/OpenAIEngine as hyperparams. Defaults are tuned
    # for weak, free, volatile models rather than benchmark maxima.
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    max_reply_tokens: int = Field(default=2048, ge=1, le=131_072)
    seed: int | None = None
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    reasoning_effort: Literal["auto", "minimal", "low", "medium", "high"] = "auto"
    json_mode: bool = False
    # How many tool round-trips a single turn may take. Weak free models loop
    # forever without a cap; 3 covers search + a follow-up.
    tool_max_rounds: int = Field(default=3, ge=1, le=10)
    # kani's self-correction budget when a tool call is malformed.
    tool_retry_attempts: int = Field(default=1, ge=0, le=5)
    # Tell the model what time it is. Without this it guesses at "today".
    # Off by default because it costs ~15 tokens per turn.
    tell_model_time: bool = False
    # Inject the built-in router guide into the chat system prompt so the
    # assistant can answer setup/status questions without guessing (R5).
    router_help: bool = True

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # File is loaded explicitly in load(); environment stays out entirely.
        return (init_settings,)

    def to_stored(self) -> dict:
        data = self.model_dump(mode="json")
        stored_providers = []
        for provider in self.providers:
            item = {
                "id": provider.id,
                "name": provider.name,
                "base_url": str(provider.base_url),
                "api_key": "",
                "models": provider.models,
                "enabled": provider.enabled,
            }
            if provider.api_key is not None:
                # Stored as-is in the local settings file. This used to be
                # Fernet-encrypted with a master.key kept in the SAME
                # directory, so anyone who could read the settings could read
                # the key too: that protected nothing. `SecretStr` still keeps
                # the value out of logs, reprs and crash dumps, which is the
                # part that actually matters for a local-only app.
                item["api_key"] = provider.api_key.get_secret_value()
            stored_providers.append(item)
        data["providers"] = stored_providers
        return data

    @classmethod
    def from_stored(cls, data: dict) -> AppSettings:
        _LOAD_WARNINGS.clear()
        data = dict(data)
        stored_providers: list[ProviderConfig] = []
        for raw in data.get("providers", []) or []:
            if not isinstance(raw, dict):
                continue
            raw = dict(raw)
            key = raw.pop("api_key", "") or ""
            if isinstance(key, str) and key:
                # Plain local storage; see to_stored() for why there is no
                # encryption layer. SecretStr keeps it out of logs.
                raw["api_key"] = SecretStr(key)
            else:
                if key:
                    LOG.warning(
                        "provider %s: stored key has wrong type, dropping key",
                        raw.get("name"),
                    )
                    name = raw.get("name", "?")
                    _LOAD_WARNINGS.append(
                        f"Stored key for provider '{name}' was unreadable. Key dropped.",
                    )
                raw["api_key"] = None
            try:
                stored_providers.append(ProviderConfig(**raw))
            except ValidationError as exc:
                LOG.warning("skipping malformed provider in stored settings: %s", exc)
                _LOAD_WARNINGS.append(
                    f"Skipped malformed provider '{raw.get('name', '?')}' from stored settings.",
                )

        data["providers"] = stored_providers

        stored_servers: list[MCPServerConfig] = []
        for raw_s in data.get("mcp_servers", []) or []:
            if not isinstance(raw_s, dict):
                continue
            try:
                stored_servers.append(MCPServerConfig(**raw_s))
            except ValidationError as exc:
                LOG.warning("skipping malformed mcp server in stored settings: %s", exc)
                _LOAD_WARNINGS.append(
                    f"Skipped malformed MCP server '{raw_s.get('name', '?')}'.",
                )

        data["mcp_servers"] = stored_servers

        try:
            return cls(**data)
        except ValidationError as exc:
            LOG.error("settings invalid (%s), starting with defaults", exc.error_count())
            _LOAD_WARNINGS.append(
                "Stored settings were invalid. Started with defaults.",
            )
            return cls()

    @classmethod
    def load(cls) -> AppSettings:
        path = storage.settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError covers JSONDecodeError AND UnicodeDecodeError (a
            # non-utf8 file is UnicodeError -> ValueError); the old tuple let
            # decode errors escape and abort startup.
            LOG.warning("settings unreadable (%s), moving aside", exc)
            _LOAD_WARNINGS.append(
                "Settings file unreadable. Started fresh. Backup: app_settings.json.bak.",
            )
            with contextlib.suppress(OSError):
                path.replace(path.with_suffix(".json.bak"))
            return cls()
        if not isinstance(data, dict):
            LOG.warning("settings file is valid JSON but not an object, moving aside")
            _LOAD_WARNINGS.append(
                "Settings file was not a JSON object. Started fresh. "
                "Backup: app_settings.json.bak.",
            )
            with contextlib.suppress(OSError):
                path.replace(path.with_suffix(".json.bak"))
            return cls()
        return cls.from_stored(data)

    def save(self) -> None:
        storage.atomic_write_json(storage.settings_path(), self.to_stored())
