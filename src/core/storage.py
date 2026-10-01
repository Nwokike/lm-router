"""Filesystem locations and atomic JSON persistence.

The three storage legs (Flet's `.flet/README.md` contract, honored here):

- **data** (`FLET_APP_STORAGE_DATA`, `base_dir()`): durable. Settings,
  conversations, their .bak. This is what survives app updates and device
  backups; anything the user would miss lives ONLY here.
- **cache** (`FLET_APP_STORAGE_CACHE`, `cache_dir()`): regenerable. The
  gateway engine snapshot (promoted via an atomic same-directory replace),
  tokenizer BPE ranks, the rotating log. The OS may purge it on device; it
  must always be rebuildable.
- **temp** (`FLET_APP_STORAGE_TEMP`, `temp_dir()`): throwaway scratch.
  Python's `tempfile` already points here under `flet run`, so any stdlib
  temp use lands correctly for free. Nothing durable may EVER be written
  here; the OS can clear it at any moment.

Deliberate non-uses of temp: atomic writes (`.tmp` promotion for JSON and
the engine) stay in the SAME directory as their target so `os.replace`
cannot fail across volumes; user exports write straight to the chosen
destination because that path is the final home, not scratch.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

from . import constants


def base_dir() -> Path:
    """App-private storage on mobile (FLET_APP_STORAGE_DATA), ~/.lm_router
    otherwise.

    This is the *data* location: settings, conversations and anything the user
    would miss. Regenerable artefacts belong in `cache_dir()`.

    NEVER Path.cwd(): a bare-python run from any directory used to scatter
    state into whatever folder the shell happened to be in (the audit found
    five stray logs plus divergent settings files). Flet's launcher always
    passes an absolute FLET_APP_STORAGE_DATA; a relative one falls through to
    the home anchor, same guard as DDGS/Sherlock.
    """
    env = os.environ.get("FLET_APP_STORAGE_DATA")
    if env:
        path = Path(env)
        if path.is_absolute():
            return path
    return Path.home() / ".lm_router"


def cache_dir() -> Path:
    """Regenerable artefacts: the cached gateway, tokenizer BPE ranks, logs.

    Flet exposes `FLET_APP_STORAGE_CACHE` alongside `FLET_APP_STORAGE_DATA`
    (see flet.controls.services.storage_paths). Keeping these apart matters on
    mobile, where the data directory is backed up: a ~100 KB engine snapshot and
    multi-megabyte tokenizer blobs do not belong in a user's backup, and
    clearing the cache is the natural way to force a fresh gateway.
    """
    env = os.environ.get("FLET_APP_STORAGE_CACHE")
    path = Path(env) if env else base_dir() / "cache"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return base_dir()
    return path


def temp_dir() -> Path:
    """Throwaway scratch space (the README's third leg).

    Under `flet run` / a packaged app this is `FLET_APP_STORAGE_TEMP`
    (sibling of data and cache, same volume). Bare runs fall back to
    `<system temp>/lm_router`: scratch must never sit next to durable
    data, and system temp is exactly as disposable as this contract gets.
    Degrades to the system temp root if even that cannot be created.
    """
    env = os.environ.get("FLET_APP_STORAGE_TEMP")
    if env:
        path = Path(env)
        if path.is_absolute():
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError:
                pass
    path = Path(tempfile.gettempdir()) / "lm_router"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir())
    return path


def settings_path() -> Path:
    return base_dir() / constants.SETTINGS_FILE


def conversations_dir() -> Path:
    path = base_dir() / constants.CONVERSATIONS_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Degrade, don't die: list_conversations runs BEFORE the first
        # paint — a bare mkdir here turned an unwritable data dir into a
        # permanently blank app (DDGS port; mirrors cache_dir above).
        return path
    return path


def engine_cache_path() -> Path:
    return cache_dir() / "engine_local.py"


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON via temp file + os.replace so a crash never truncates state.

    Files land 0600 (owner-only): settings carry provider API keys and the
    share key, and the inherited umask left them world-readable on default
    POSIX setups. No-ops on Windows (chmod is a no-op there anyway).
    """
    import stat

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
