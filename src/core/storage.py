"""Filesystem locations and atomic JSON persistence."""

import json
import os
from pathlib import Path

from . import constants


def base_dir() -> Path:
    """App-private storage on mobile (FLET_APP_STORAGE_DATA), cwd on desktop.

    This is the *data* location: settings, conversations and anything the user
    would miss. Regenerable artefacts belong in `cache_dir()`.
    """
    env = os.environ.get("FLET_APP_STORAGE_DATA")
    if env:
        return Path(env)
    return Path.cwd()


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


def settings_path() -> Path:
    return base_dir() / constants.SETTINGS_FILE


def conversations_dir() -> Path:
    path = base_dir() / constants.CONVERSATIONS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def engine_cache_path() -> Path:
    return cache_dir() / "engine_local.py"


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON via temp file + os.replace so a crash never truncates state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return default
    except json.JSONDecodeError:
        return default
