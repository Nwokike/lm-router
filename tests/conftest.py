"""Shared fixtures: pinned asyncio backend for the anyio pytest plugin."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
