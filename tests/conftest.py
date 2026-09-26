"""Shared fixtures: pinned asyncio backend for the anyio pytest plugin."""

import os
from pathlib import Path

import pytest

# Pin tiktoken to the repo's existing BPE cache BEFORE core imports: the
# home-dir anchor for bare runs (core/__init__) would be empty here and force
# a 5MB network download during tests.
os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / "tiktoken_cache"),
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
