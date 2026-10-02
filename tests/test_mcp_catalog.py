"""Curated MCP catalog: wellformed presets, safe copy, payload building."""

import json

from services.mcp_catalog import PRESETS, preset_payload

# kiri-router's gateway tokens are banned from this app's src entirely
# (test_router_invariants) — including inside the catalog's copy.
BANNED = ["opencode", "big-pickle", "platform.kiri.ng", "kilo", "llm7", "kepler"]


def test_presets_are_wellformed_and_carry_no_gateway_tokens() -> None:
    names: set[str] = set()
    for preset in PRESETS:
        assert preset["url"].startswith("https://"), preset
        assert preset["name"].strip(), preset
        assert len(preset["description"]) > 20, preset
        blob = json.dumps(preset).lower()
        for token in BANNED:
            assert token not in blob, f"{preset['name']} carries {token!r}"
        assert preset["name"] not in names, f"duplicate name {preset['name']}"
        names.add(preset["name"])

    keyless = [p for p in PRESETS if not p.get("auth")]
    keyed = [p for p in PRESETS if p.get("auth")]
    # The gallery leads with zero-setup entries; GitHub and friends follow.
    assert len(keyless) >= 6, f"only {len(keyless)} keyless presets"
    assert len(keyed) >= 4, f"only {len(keyed)} keyed presets"
    for preset in keyed:
        auth = preset["auth"]
        assert auth["get_key_url"].startswith("https://"), preset
        assert auth["key_label"], preset


def test_payload_builder_sends_exactly_the_right_headers() -> None:
    by_id = {p["id"]: p for p in PRESETS}

    # Keyless: never sends headers, transports the url verbatim.
    # The builtin Exa preset additionally carries its stable id + protected
    # marker so the seeded entry can't be deleted.
    payload = preset_payload(by_id["exa"])
    assert payload == {
        "id": "builtin-exa",
        "name": "Exa",
        "transport": "streamable_http",
        "url": "https://mcp.exa.ai/mcp",
        "headers": {},
        "protected": True,
    }

    # Keyed + key: the standard bearer header GitHub's endpoint reads.
    payload = preset_payload(by_id["github"], " github_pat_123 ")
    assert payload["headers"] == {"Authorization": "Bearer github_pat_123"}
    assert payload["url"] == "https://api.githubcopilot.com/mcp/"  # trailing slash kept

    # Keyed without a key (caller should have blocked this): no empty header.
    payload = preset_payload(by_id["tavily"], "")
    assert payload["headers"] == {}
    # ...and a key on a keyless preset is ignored, not leaked.
    payload = preset_payload(by_id["context7"], "sk-should-not-appear")
    assert payload["headers"] == {}
