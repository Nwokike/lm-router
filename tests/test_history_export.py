"""History service: timestamps, Markdown/JSON export, slugified filenames."""

import json
from pathlib import Path

from services import history


def _iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))


def _write_conversation(tmp_path: Path, conv_id: str, messages: list[dict]) -> Path:
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"{conv_id}.json"
    payload = {"always_included_messages": [], "chat_history": messages}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_list_conversations_includes_relative_and_updated(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    _write_conversation(
        tmp_path,
        "abc123def456",
        [
            {"role": "user", "content": "What is Flet?"},
            {"role": "assistant", "content": "Flet is a Python UI framework."},
        ],
    )

    items = history.list_conversations()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "abc123def456"
    assert item["title"] == "What is Flet?"
    assert isinstance(item["relative"], str) and item["relative"]
    assert isinstance(item["updated"], str) and item["updated"]
    assert "mtime" in item


def test_export_conversation_markdown_roundtrip(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    _write_conversation(
        tmp_path,
        "exporttest01",
        [
            {"role": "user", "content": "Explain kani function calling!"},
            {"role": "assistant", "content": "Kani wraps LLM function calls cleanly."},
            {"role": "function", "name": "search", "content": "search result text"},
        ],
    )

    content, filename = history.export_conversation_markdown("exporttest01")
    assert "# Explain kani function calling!" in content
    assert "### User" in content
    assert "### Assistant" in content
    assert "#### Tool (search)" in content
    assert "search result text" in content
    assert filename.endswith(".md")
    # Slugified: no spaces, lowercase, safe characters only
    assert " " not in filename
    assert filename == "explain-kani-function-calling.md"


def test_export_conversation_json_roundtrip(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    _write_conversation(
        tmp_path,
        "jsonexport01",
        [{"role": "user", "content": "JSON export test"}],
    )

    content, filename = history.export_conversation_json("jsonexport01")
    data = json.loads(content)
    assert data["chat_history"][0]["content"] == "JSON export test"
    assert filename.endswith(".json")
    assert " " not in filename


def test_export_missing_conversation_returns_empty(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    md, md_name = history.export_conversation_markdown("does-not-exist")
    assert md == ""
    assert md_name.endswith(".md")

    js, js_name = history.export_conversation_json("does-not-exist")
    assert js == "{}"
    assert js_name.endswith(".json")


def test_slugify_handles_special_characters_and_long_titles(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    long_title = "How do I configure a custom OpenAI-compatible provider with HTTPS?! #setup guide"
    _write_conversation(
        tmp_path,
        "sluggedges01",
        [{"role": "user", "content": long_title}],
    )

    _, filename = history.export_conversation_markdown("sluggedges01")
    assert filename.endswith(".md")
    assert " " not in filename
    assert "!" not in filename
    assert "#" not in filename
    # max_length=50 keeps filenames short
    assert len(filename) <= 55


def test_delete_and_clear_all(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    _write_conversation(tmp_path, "delme0000001", [{"role": "user", "content": "bye"}])
    _write_conversation(tmp_path, "delme0000002", [{"role": "user", "content": "also bye"}])
    assert len(history.list_conversations()) == 2

    history.delete_conversation("delme0000001")
    remaining = history.list_conversations()
    assert len(remaining) == 1
    assert remaining[0]["id"] == "delme0000002"

    history.clear_all()
    assert history.list_conversations() == []
