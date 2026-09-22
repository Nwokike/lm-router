"""Filesystem locations and atomic JSON persistence."""

import json
import os
from pathlib import Path

from . import constants


def base_dir() -> Path:
    """App-private storage on mobile (FLET_APP_STORAGE_DATA), cwd on desktop."""
    env = os.environ.get("FLET_APP_STORAGE_DATA")
    if env:
        return Path(env)
    return Path.cwd()


def settings_path() -> Path:
    return base_dir() / constants.SETTINGS_FILE


def master_key_path() -> Path:
    return base_dir() / constants.MASTER_KEY_FILE


def conversations_dir() -> Path:
    path = base_dir() / constants.CONVERSATIONS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


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
