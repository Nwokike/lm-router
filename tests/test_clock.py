"""Current date/time delivery: system-prompt line plus an on-demand tool.

The clock is ALWAYS on: a model that guesses at "today" is worse than one
that never sees the date, and ~15 tokens per turn is a price nobody should
have to discover a toggle to pay.
"""

from __future__ import annotations

import asyncio
import re

from core.settings import AppSettings
from services.clock import build_time_tool, now, prompt_line, with_clock


def test_prompt_line_carries_a_real_timestamp() -> None:
    line = prompt_line()
    assert line.startswith("Current date and time:")
    # ISO-ish date, so a model can read it unambiguously.
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)


def test_clock_is_always_on_no_settings_toggle() -> None:
    """No toggle: the legacy field only exists for old saved configs."""
    settings = AppSettings()
    assert not hasattr(settings, "tell_model_time") or settings.tell_model_time


def test_with_clock_always_appends() -> None:
    once = with_clock("Be brief.")
    assert once.startswith("Be brief.")
    assert "Current date and time:" in once
    # Works with an empty prompt too.
    assert with_clock("").startswith("Current date and time:")


def test_with_clock_rewrap_is_idempotent() -> None:
    once = with_clock("Be brief.")
    assert once.count("Current date and time:") == 1
    # A second wrap a moment later must replace, not stack.
    twice = with_clock(once)
    assert twice.count("Current date and time:") == 1


def test_time_tool_is_registered_for_kani() -> None:
    tool = build_time_tool()
    assert tool.name == "current_time"
    assert tool.json_schema["type"] == "object"


def test_time_tool_answers_for_local_and_named_zone() -> None:
    tool = build_time_tool()
    local = asyncio.run(tool())
    assert "Current date and time:" in local

    named = asyncio.run(build_time_tool()("Africa/Lagos"))
    assert "Africa/Lagos" in named


def test_unknown_timezone_degrades_to_local_time() -> None:
    """A bad zone must not fail the turn — the model still gets an answer."""
    result = asyncio.run(build_time_tool()("Not/AZone"))
    assert "Device local time" in result
    assert "Current date and time:" in result


def test_now_is_timezone_aware() -> None:
    assert now().tzinfo is not None
