"""Bundled changelog shown by the version dialog when the app is up to
date — works fully offline. One entry per release; keep the entry for the
current APP_VERSION in sync when bumping.

CI consumes the same source (scripts/generate_release_notes.py ->
release_notes.md -> the GitHub release body), so the release page, the
in-app What's New and the app itself can never disagree.
"""

CHANGELOG: dict[str, str] = {
    "1.0.0": (
        "• Your own Kiri Router gateway, fetched live and running on this device — no setup, no API key\n"
        "• Live model catalog with per-model rate limits and endpoint types\n"
        "• Streaming chat with visible reasoning and expandable tool calls\n"
        "• Share the gateway on a public URL with an optional API key\n"
        "• On-device conversation history with markdown export"
    ),
    "1.1.0": (
        "• Both close buttons confirm before quitting; the gateway survives minimizing\n"
        "• MCP servers: curated one-tap catalog plus your own stdio/remote servers\n"
        "• Live activity log terminal and per-model Test verdicts\n"
        "• Flet 1.0.4 runtime with hardened boot and connectivity handling"
    ),
    "1.2.0": (
        "• Gateway-first rebuild: Server is the homepage — start the gateway, share it, watch the catalog\n"
        "• New technical Test tab: one-tap model probes with the raw REQUEST and RESPONSE JSON, collapsible reasoning, and the DDGS-style composer (Enter sends, Shift+Enter breaks the line)\n"
        "• Background keep-alive: a foreground service + wake lock keep the gateway alive while the app is minimized, with a notification Stop action\n"
        "• Slimmer app: kani, OpenAI, MCP, pydantic and tiktoken removed — pure-stdlib settings that survive corrupt files\n"
        "• Python 3.14 runtime across every platform build"
    ),
}


def notes_for(version: str) -> str:
    """Changelog entry for a version, falling back to the latest entry."""
    return CHANGELOG.get(version) or next(reversed(CHANGELOG.values()), "")
