"""Current date and time, always delivered — models must never guess "today".

Two delivery routes, because each misses cases the other catches:

* a short line appended to the system prompt, so everyday questions ("what is
  the latest X?") are answered correctly without a round trip; and
* a `current_time` tool, for when the model needs an exact reading (time zone,
  day of week) and would rather ask than rely on a prompt line that may be
  stale within a long session.

~15 tokens per turn, always on.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from kani import AIParam
from kani.ai_function import AIFunction


def now() -> datetime:
    try:
        return datetime.now().astimezone()
    except OSError, ValueError:
        return datetime.now(tz=UTC)


def prompt_line() -> str:
    """One line the model can rely on, e.g.
    `Current date and time: 2026-09-24 18:42:31 (UTC+01:00, Thursday).`
    """
    moment = now()
    offset = moment.strftime("%z")
    if len(offset) == 5:  # +0100 -> +01:00
        offset = f"{offset[:3]}:{offset[3:]}"
    zone = moment.tzname() or "local"
    return (
        f"Current date and time: {moment.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({offset} {zone}, {moment.strftime('%A')})."
    )


_CLOCK_LINE_RE = re.compile(r"(?m)^Current date and time:.*(?:\n|$)")


def strip_clock_line(text: str) -> str:
    """The prompt minus any clock line.

    The line embeds seconds, so callers that compare prompts for equality
    (engine reuse) must strip it first or the comparison fails every send.
    """
    return _CLOCK_LINE_RE.sub("", (text or "")).strip()


def with_clock(system_prompt: str) -> str:
    """Append the current date/time line to the system prompt, always.

    Idempotent: a re-wrap replaces any stale line instead of stacking.
    """
    base = strip_clock_line(system_prompt)
    line = prompt_line()
    return f"{base}\n\n{line}" if base else line


def build_time_tool() -> AIFunction:
    """A kani tool so the model can ask for the exact time on demand."""

    async def current_time(
        timezone_name: Annotated[
            str,
            AIParam("IANA timezone name, e.g. Africa/Lagos. Omit for the device's local time."),
        ] = "",
    ) -> str:
        """Get the current date and time. Use this whenever the answer depends
        on what "now" is — today's date, the current time, or anything
        time-relative like "latest" or "up to date"."""
        if timezone_name:
            try:
                from zoneinfo import ZoneInfo

                moment = datetime.now(ZoneInfo(timezone_name))
                return f"{moment.isoformat()} ({timezone_name})"
            except Exception:
                # An unknown zone must not fail the turn; fall back to local.
                return f"Unknown timezone {timezone_name!r}. Device local time is {prompt_line()}"
        return prompt_line()

    return AIFunction(current_time, name="current_time", auto_retry=False)
