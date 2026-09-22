"""Typed application settings with JSON persistence and encrypted keys.

Environment/dotenv sources are deliberately disabled via
settings_customise_sources (the pydantic-settings FASTMCP_ lesson): init
values are the only input, the file on disk is managed explicitly below, so
no ambient env var can silently override user choices.
"""

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

from . import constants, secrets, storage
from .logging import LOG


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, max_length=64)
    base_url: AnyUrl
    api_key: SecretStr | None = None
    models: list[str] = []
    enabled: bool = True


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, max_length=64)
    transport: Literal["stdio", "sse", "streamable_http"] = "streamable_http"
    command: str | None = None
    args: list[str] = []
    url: AnyUrl | None = None
    headers: dict[str, str] = {}
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

    theme: Literal["system", "light", "dark"] = "system"
    onboarding_done: bool = False
    terms_accepted: bool = False
    system_prompt: str = (
        "You are LM Router, a helpful assistant running locally on the user's device."
    )
    last_model: str = ""
    gateway_port: int = Field(default=constants.DEFAULT_GATEWAY_PORT, ge=1, le=65535)
    gateway_autostart: bool = True
    search_enabled: bool = True
    interstitial_every: int = Field(default=constants.INTERSTITIAL_EVERY, ge=1)
    providers: list[ProviderConfig] = []
    active_provider_id: str = ""
    mcp_servers: list[MCPServerConfig] = []
    max_context_tokens: int = Field(default=128_000, ge=1024)
    update_check: bool = True

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
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
                item["api_key"] = secrets.encrypt(provider.api_key.get_secret_value())
            stored_providers.append(item)
        data["providers"] = stored_providers
        return data

    @classmethod
    def from_stored(cls, data: dict) -> AppSettings:
        data = dict(data)
        stored_providers = []
        for raw in data.get("providers", []):
            raw = dict(raw)
            token = raw.pop("api_key", "") or ""
            if token:
                try:
                    raw["api_key"] = SecretStr(secrets.decrypt(token))
                except ValueError:
                    LOG.warning("provider %s: stored key unreadable, dropping key", raw.get("name"))
                    raw["api_key"] = None
            else:
                raw["api_key"] = None
            stored_providers.append(raw)
        data["providers"] = stored_providers
        try:
            return cls(**data)
        except ValidationError as exc:
            LOG.error("settings invalid (%s), starting with defaults", exc.error_count())
            return cls()

    @classmethod
    def load(cls) -> AppSettings:
        path = storage.settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("settings unreadable (%s), moving aside", exc)
            try:
                path.replace(path.with_suffix(".json.bak"))
            except OSError:
                pass
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_stored(data)

    def save(self) -> None:
        storage.atomic_write_json(storage.settings_path(), self.to_stored())
