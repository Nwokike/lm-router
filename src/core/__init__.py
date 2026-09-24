"""Core package bootstrap.

Runs before any kani/tiktoken import (the app_shell import chain reaches
core first): pin a per-install tiktoken BPE cache so encoding downloads
happen once and survive restarts, instead of re-downloading into %TEMP%
on every launch (and blocking the UI thread on a cold cache).
"""

import os
from pathlib import Path


def _pin_tiktoken_cache() -> None:
    if os.environ.get("TIKTOKEN_CACHE_DIR") or os.environ.get("DATA_GYM_CACHE_DIR"):
        return
    base = os.environ.get("FLET_APP_STORAGE_CACHE") or str(Path.cwd())
    try:
        path = Path(base) / "tiktoken_cache"
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    os.environ["TIKTOKEN_CACHE_DIR"] = str(path)


_pin_tiktoken_cache()
