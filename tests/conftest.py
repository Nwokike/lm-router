"""Shared fixtures for the lm-router test suite."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
