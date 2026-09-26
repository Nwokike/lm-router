"""The router's secrecy contract applied to OUR sources.

Mirrors kiri-router's build-time scan (tools/bundle-scripts.js FORBIDDEN +
docs/invariants.md 3/11): gateway tokens never appear anywhere in the app,
and the ROUTER's version is never rendered on a user-facing surface (uptime
and counts are the whole health story). The token list lives only here —
tests are never shipped.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

FORBIDDEN = [
    "opencode",
    "big-pickle",
    "platform.kiri.ng",
    "kilo",
    "llm7",
    "kepler",
]


def test_no_gateway_tokens_anywhere_in_src() -> None:
    """Case-insensitive substring scan, same strictness as the router's own
    build gate — comments and docstrings included: our code must not even
    NAME upstreams."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert not offenders, "gateway tokens found in src/:\n" + "\n".join(offenders)


def test_router_version_is_never_rendered() -> None:
    """Invariant 11 / D9: no version number of the ROUTER on any screen.
    (The app's OWN version in About is fine — that is lm-router's, not the
    router's; engine/logging keep gateway_version for diagnostics.)"""
    offenders: list[str] = []
    for folder in ("screens", "components"):
        for path in sorted((SRC / folder).rglob("*.py")):
            if "gateway_version" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    shell = SRC / "app_shell.py"
    if "gateway_version" in shell.read_text(encoding="utf-8"):
        offenders.append("src/app_shell.py")
    assert not offenders, "router version rendered in UI: " + ", ".join(offenders)
