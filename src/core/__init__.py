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
    # NEVER Path.cwd(): bare runs used to drop a multi-megabyte BPE cache
    # into whatever directory the shell was in (two such dirs existed in the
    # audit). Flet's launcher sets the cache env var; bare runs anchor to the
    # app's home directory, next to base_dir()'s data.
    env = os.environ.get("FLET_APP_STORAGE_CACHE")
    base = Path(env) if env and Path(env).is_absolute() else Path.home() / ".lm_router" / "cache"
    try:
        path = base / "tiktoken_cache"
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only dir: tiktoken falls back to its temp cache; the prewarm
        # log reports the heuristic registry instead of failing boot.
        return
    os.environ["TIKTOKEN_CACHE_DIR"] = str(path)


_pin_tiktoken_cache()
