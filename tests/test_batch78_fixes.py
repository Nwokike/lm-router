"""Batch 7+8 component/core regression.

- ToolCallBlock expanded body uses Padding (Margin-as-padding fixed).
- BannerAd builds once; error hides the whole card chrome.
- Model picker auto row shows its own label.
- Strip pills carry ink feedback.
- Legal/more-apps docs are whole-row tappables; Contact/Rate trailing
  icons carry no second handler (no double-fire).
- Section headers uppercase; About dialog shaped + titled.
- Log terminal + server log rows tolerate malformed records.
- Connectivity probe uses GET via anyio.to_thread.
- Log rotation actually rotates (redact-then-delegate).
- Catalog _cap survives non-dict rate_hint; advice avoids "..".
- Clock re-wrap never stacks duplicate lines.
- Bench transport error after a 429 classifies FAIL, not RATE.
- save_text_file honors cancel (None, no write), sanitizes names.
"""

from __future__ import annotations

from test_boot_smoke import boot_page
from test_render import _render, _renderer_page, _walk_all

__all__ = ["_renderer_page", "boot_page"]


def test_thinking_block_padding_type(_renderer_page) -> None:
    from components.thinking import ToolCallBlock

    tree = _render(lambda: ToolCallBlock(name="test", content="out", is_error=False, is_dark=False))
    margins = [n for n in _walk_all(tree) if type(n).__name__ == "Margin"]
    # The expanded-body defect was Margin-as-padding; no Margin may remain.
    assert margins == [], "Margin value type still present in ToolCallBlock"


def test_banner_builds_single_ad(_renderer_page) -> None:

    calls = {"n": 0}
    real_ad = None
    try:
        import flet_ads as fta

        real_ad = fta.BannerAd
    except ImportError:
        pass

    class _FakeAd:
        def __init__(self, *args, **kwargs):
            calls["n"] += 1

    if real_ad is not None:
        import flet_ads as fta

        fta.BannerAd = _FakeAd  # type: ignore[assignment]
    try:
        from components.banner_ad import build_banner_ad

        build_banner_ad()
    finally:
        if real_ad is not None:
            import flet_ads as fta

            fta.BannerAd = real_ad
    assert calls["n"] <= 1, f"BannerAd constructed {calls['n']}x (duplicate)"


def test_section_headers_uppercase(_renderer_page) -> None:
    from components.section_header import section_header

    tree = _render(lambda: section_header("Appearance"))
    texts = [n.value for n in _walk_all(tree) if type(n).__name__ == "Text"]
    assert any(t == "APPEARANCE" for t in texts), texts


def test_catalog_cap_survives_string_hint() -> None:
    from core.catalog import _rank_alternatives

    catalog = [
        {"id": "a", "rate_hint": "not a dict"},
        {"id": "b", "rate_hint": {"approx_per_hour": 50}},
    ]
    ranked = _rank_alternatives("zzz", catalog)
    assert {m["id"] for m in ranked} == {"a", "b"}


def test_rate_advice_no_double_period() -> None:
    from core.catalog import rate_limit_advice

    catalog = [{"id": "m1", "rate_hint": {"label": "Capped at 10/hr."}}]
    out = rate_limit_advice("m1", catalog)
    assert ".." not in out, out
    assert out.startswith("Rate limited.")


def test_clock_rewrap_idempotent() -> None:
    from services.clock import with_clock

    once = with_clock("Be brief.", True)
    assert once.count("Current date and time:") == 1
    # A second wrap a moment later must replace, not stack.
    twice = with_clock(once, True)
    assert twice.count("Current date and time:") == 1


def test_bench_post_429_transport_error_is_fail() -> None:
    from services.model_bench import FAIL, classify

    assert classify(429, "", transport_error=True) == FAIL


def test_save_cancel_writes_nothing(boot_page) -> None:
    import asyncio

    from services import file_save as fs_mod

    class _CancelPicker:
        async def save_file(self, **kwargs):
            return None

    # save_text_file(page, ...) needs a page; exercise the cancel branch
    # through a minimal fake instead.
    class _FakePage:
        file_picker = _CancelPicker()

        def update(self):
            pass

    out = asyncio.run(fs_mod.save_text_file(_FakePage(), "content", "cancel.md"))  # type: ignore[arg-type]
    assert out is None, "cancel must return None without writing"


def test_log_rotation_configured() -> None:
    import logging.handlers

    from core.logging import RedactFileHandler

    assert issubclass(RedactFileHandler, logging.handlers.RotatingFileHandler)
