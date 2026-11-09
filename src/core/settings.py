"""Application settings with JSON persistence — pure stdlib.

Zero third-party validation: dataclass-flavoured plain classes with
explicit checks. Everything pydantic did here (field validation on
assignment, load warnings, atomic save) is reproduced below with the same
public surface, so callers and tests keep working unchanged.

Environment/dotenv sources were deliberately disabled even under pydantic
(the FASTMCP_ lesson): init values are the only input, the file on disk is
managed explicitly below, so no ambient env var can silently override user
choices.
"""

import contextlib
import json

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


class ValidationError(ValueError):
    """A settings value failed validation; str(exc) is user-facing."""


_THEMES = ("system", "light", "dark")


class AppSettings:
    """The app's persisted settings file, one attribute per user choice."""

    __slots__ = (
        "gateway_autostart",
        "gateway_port",
        "interstitial_every",
        "last_model",
        "onboarding_done",
        "require_share_key",
        "settings_version",
        "share_enabled",
        "share_key",
        "terms_accepted",
        "theme",
        "update_check",
    )

    def __init__(self, **data: object) -> None:
        self.settings_version = 1
        self.theme = "system"
        self.onboarding_done = False
        self.terms_accepted = False
        self.last_model = ""
        self.gateway_port = constants.DEFAULT_GATEWAY_PORT
        self.gateway_autostart = True
        # share_enabled: publish this device's gateway through a public
        # tunnel. Off by default; the gateway itself has no auth, so turning
        # this on with require_share_key=False is an open relay by choice.
        self.share_enabled = False
        self.require_share_key = False
        self.share_key = ""
        self.interstitial_every = constants.INTERSTITIAL_EVERY
        self.update_check = True
        for key, value in data.items():
            if key in AppSettings.__slots__:
                setattr(self, key, value)
            # Unknown keys are ignored (stored files may carry removed ones).

    def __setattr__(self, name: str, value: object) -> None:
        # validate_assignment parity: the constrained fields are checked on
        # EVERY assignment, so a bad value can never reach save().
        if name == "gateway_port":
            try:
                port = int(value)  # type: ignore[arg-type]
            except TypeError, ValueError:
                raise ValidationError("Port must be a number from 1 to 65535.") from None
            if not 1 <= port <= 65535:
                raise ValidationError("Port must be a number from 1 to 65535.")
            value = port
        elif name == "theme":
            if value not in _THEMES:
                raise ValidationError("Theme must be system, light, or dark.")
        elif name == "interstitial_every":
            try:
                every = int(value)  # type: ignore[arg-type]
            except TypeError, ValueError:
                raise ValidationError("Interstitial interval must be 1 or more.") from None
            if every < 1:
                raise ValidationError("Interstitial interval must be 1 or more.")
            value = every
        object.__setattr__(self, name, value)

    def to_stored(self) -> dict:
        return {
            "settings_version": self.settings_version,
            "theme": self.theme,
            "onboarding_done": self.onboarding_done,
            "terms_accepted": self.terms_accepted,
            "last_model": self.last_model,
            "gateway_port": self.gateway_port,
            "gateway_autostart": self.gateway_autostart,
            "share_enabled": self.share_enabled,
            "require_share_key": self.require_share_key,
            "share_key": self.share_key,
            "interstitial_every": self.interstitial_every,
            "update_check": self.update_check,
        }

    @classmethod
    def from_stored(cls, data: dict) -> AppSettings:
        _LOAD_WARNINGS.clear()
        data = dict(data)

        known = {k: v for k, v in data.items() if k in AppSettings.__slots__}
        try:
            return cls(**known)
        except ValidationError as exc:
            # A hand-corrupted scalar (theme: null, port: 0) must degrade to
            # defaults, exactly like a malformed provider row does — never
            # abort boot (verifier finding #24, 2026-11-09). The warning is
            # appended AFTER the recursive from_stored (which clears the
            # list on entry), so the message actually reaches the UI.
            LOG.error("settings invalid (%s), starting with defaults", exc)
            fallback = cls.from_stored({})
            _LOAD_WARNINGS.append("Stored settings were invalid. Started with defaults.")
            return fallback

    @classmethod
    def load(cls) -> AppSettings:
        path = storage.settings_path()
        if not path.exists():
            # from_stored({}), NOT cls(): every construction path must run
            # the builtin seeding. The bare-cls early returns booted fresh
            # installs with zero servers — no Exa, no error, nothing in the
            # log (owner run 2026-10-02: second launch had no MCP at all).
            return cls.from_stored({})
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
            return cls.from_stored({})
        if not isinstance(data, dict):
            LOG.warning("settings file is valid JSON but not an object, moving aside")
            _LOAD_WARNINGS.append(
                "Settings file was not a JSON object. Started fresh. "
                "Backup: app_settings.json.bak.",
            )
            with contextlib.suppress(OSError):
                path.replace(path.with_suffix(".json.bak"))
            return cls.from_stored({})
        return cls.from_stored(data)

    def save(self) -> None:
        storage.atomic_write_json(storage.settings_path(), self.to_stored())
