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


# --- durability: tombstones, atomicity, retention (DDGS-ported) -------------

import os  # noqa: E402  (kept with the durability block it serves)

from core import storage  # noqa: E402
from core.state import state  # noqa: E402


class _FakeKani:
    """Just enough kani: save writes the SavedKani JSON shape."""

    def __init__(self) -> None:
        self.chat_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ]
        self.save_calls: list[tuple[str, str | None]] = []

    def save(self, fp, *, save_format=None, **kwargs) -> None:
        # The format must be pinned: kani infers ZIP from a ".tmp" suffix.
        self.save_calls.append((str(fp), save_format))
        Path(fp).write_text(
            json.dumps(
                {"always_included_messages": [], "chat_history": self.chat_history},
            ),
            encoding="utf-8",
        )


class _FakeAgent:
    def __init__(self) -> None:
        self.kani = _FakeKani()


def test_a_pending_save_cannot_resurrect_a_deleted_chat(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    history._tombstones.clear()
    agent = _FakeAgent()
    state.active_conversation = "tomb00000001"
    try:
        assert history.save_conversation(agent) is True
        path = history.conversation_path("tomb00000001")
        assert path.exists()
        calls_before = len(agent.kani.save_calls)
        assert calls_before == 1

        assert history.delete_conversation("tomb00000001") is True
        assert not path.exists()

        # The save that was already in flight now replays — and must refuse.
        # Returns True (delete intent wins; no bogus "not saved" toast).
        assert history.save_conversation(agent) is True
        assert not path.exists(), "delete must not be undone"
        # Atomic write leaves no .tmp behind either way.
        assert not list(tmp_path.glob("conversations/*.tmp"))
        assert len(agent.kani.save_calls) == calls_before, "tombstoned id must not reach kani.save"
    finally:
        state.active_conversation = ""


def test_delete_reports_success_for_missing_and_twice(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    history._tombstones.clear()
    # Never existed: the user's intent is satisfied.
    assert history.delete_conversation("never-existed-id") is True
    # Gone already: double-click must not surface a failure toast.
    path = _write_conversation(tmp_path, "twice0000001", [{"role": "user", "content": "x"}])
    assert history.delete_conversation("twice0000001") is True
    assert not path.exists()
    assert history.delete_conversation("twice0000001") is True


def test_clear_all_tombstones_every_id(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    history._tombstones.clear()
    _write_conversation(tmp_path, "clear00000001", [{"role": "user", "content": "a"}])
    state.active_conversation = "clear00000002"
    _write_conversation(tmp_path, "clear00000002", [{"role": "user", "content": "b"}])
    try:
        agent = _FakeAgent()
        assert history.save_conversation(agent) is True
        assert history.clear_all() == 0
        assert history.list_conversations() == []

        assert history.save_conversation(agent) is True
        assert not history.conversation_path("clear00000002").exists(), (
            "clear_all must not be undone by a pending save"
        )
    finally:
        state.active_conversation = ""


def test_prune_keeps_fifty_and_never_the_active(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    history._tombstones.clear()
    base = 1_700_000_000
    for index in range(55):
        path = _write_conversation(
            tmp_path,
            f"conv{index:010d}",
            [{"role": "user", "content": f"m{index}"}],
        )
        stamp = base + index  # distinct mtimes: conv000 is the OLDEST
        os.utime(path, (stamp, stamp))

    # The active conversation is the OLDEST file — it must still survive.
    state.active_conversation = "conv0000000000"
    try:
        agent = _FakeAgent()
        assert history.save_conversation(agent) is True
        remaining = history.list_conversations()
        assert len(remaining) == history.MAX_CONVERSATIONS
        ids = {item["id"] for item in remaining}
        assert "conv0000000000" in ids, "active conversation must never be pruned"
        assert "conv0000000054" in ids, "newest conversation must survive"
        assert "conv0000000001" not in ids, "the oldest unprotected chat goes first"
    finally:
        state.active_conversation = ""


def test_conversation_path_stays_inside_the_conversations_dir(
    tmp_path,
    monkeypatch,
) -> None:
    _iso(tmp_path, monkeypatch)
    hostile = history.conversation_path("../../evil")
    assert hostile.parent == storage.conversations_dir()
    assert ".." not in hostile.name
    assert hostile.name == "evil.json"


def test_title_collapses_internal_whitespace(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    _write_conversation(
        tmp_path,
        "titlex0000001",
        [{"role": "user", "content": "line one\nline two   with spaces"}],
    )
    items = history.list_conversations()
    assert items[0]["title"] == "line one line two with spaces"


def test_conversations_dir_survives_an_unwritable_parent(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)

    def _boom(*_a, **_k) -> None:
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", _boom)
    # Neither the dir helper nor the boot-time listing may raise.
    assert isinstance(storage.conversations_dir(), Path)
    assert history.list_conversations() == []
