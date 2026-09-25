"""Current date/time delivery: system-prompt line plus an on-demand tool."""

from __future__ import annotations

import asyncio
import re

from services.clock import build_time_tool, now, prompt_line, with_clock


def test_prompt_line_carries_a_real_timestamp() -> None:
    line = prompt_line()
    assert line.startswith("Current date and time:")
    # ISO-ish date, so a model can read it unambiguously.
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)


def test_clock_is_off_by_default_in_settings() -> None:
    from core.settings import AppSettings

    assert AppSettings().tell_model_time is False


def test_with_clock_is_a_no_op_when_disabled() -> None:
    assert with_clock("Be brief.", False) == "Be brief."
    assert with_clock("", False) == ""


def test_with_clock_appends_exactly_one_line() -> None:
    once = with_clock("Be brief.", True)
    assert once.startswith("Be brief.")
    assert "Current date and time:" in once
    # Idempotent: a re-wrap must not stack duplicate clock lines.
    twice = with_clock(once, True)
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
