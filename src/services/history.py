"""Conversation persistence and export: kani save/load files under conversations/."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from core import storage
from core.logging import LOG
from core.state import state
from services.agent import AgentService


def _humanize(when: datetime) -> str:
    """Relative time in the house style ("just now", "2 hours ago")."""
    delta = datetime.now().astimezone() - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    return f"{days // 365} year{'s' if days // 365 != 1 else ''} ago"


def _slugify(title: str) -> str:
    """Filesystem-safe export filename stem. ASCII, lowercase, hyphenated."""
    lowered = unicodedata.normalize("NFKD", str(title)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered.lower()).strip("-")
    return slug[:50].strip("-")


def _title_from_file(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return path.stem
    # A hand-edited or truncated file can hold a list/scalar/null instead of the
    # expected object; list_conversations() runs during boot, so anything raised
    # here would abort startup before the first paint.
    if not isinstance(data, dict):
        return path.stem
    history = data.get("chat_history")
    if not isinstance(history, list):
        return path.stem
    for message in history:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()[:48]
    return path.stem


def list_conversations() -> list[dict]:
    directory = storage.conversations_dir()
    items = []
    for path in directory.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            LOG.warning("conversation file unreadable (%s): %s", path.name, exc)
            continue
        try:
            when = datetime.fromtimestamp(mtime).astimezone()
            relative = _humanize(when)
            updated = when.strftime("%Y-%m-%d %H:%M")
        except Exception as exc:
            LOG.warning("timestamp formatting failed for %s: %s", path.name, exc)
            relative = "recently"
            updated = ""
        items.append(
            {
                "id": path.stem,
                "title": _title_from_file(path),
                "relative": relative,
                "updated": updated,
                "mtime": mtime,
                "path": str(path),
            },
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def start_conversation() -> str:
    state.active_conversation = uuid.uuid4().hex[:12]
    return state.active_conversation


def conversation_path(conversation_id: str) -> Path:
    return storage.conversations_dir() / f"{conversation_id}.json"


def save_conversation(agent: AgentService) -> bool:
    """Persist the active conversation; False when the save failed (surfaced
    by the caller — a silently lost conversation is user-visible data loss).

    Pure IO: state.conversations is refreshed by the caller on the UI loop.
    """
    if not state.active_conversation or agent.kani is None:
        return True
    try:
        agent.kani.save(str(conversation_path(state.active_conversation)))
    except Exception as exc:
        LOG.warning("conversation save failed: %s", exc)
        return False
    return True


def load_conversation(
    agent: AgentService, conversation_id: str, *, apply_state: bool = True
) -> bool:
    """Load a saved conversation into the agent.

    apply_state=False keeps this pure IO/kani work so callers can run it on a
    worker thread; the controller then applies state in the Flet context
    (forensics R5: observable writes off-context can fail silently).
    """
    path = conversation_path(conversation_id)
    if not path.exists() or agent.kani is None:
        return False
    try:
        agent.kani.load(str(path))
    except Exception as exc:
        LOG.warning("conversation load failed: %s", exc)
        return False
    if apply_state:
        state.active_conversation = conversation_id
        state.messages = messages_from_history(agent)
    LOG.info("loaded conversation %s (%d messages)", conversation_id, len(agent.kani.chat_history))
    return True


def messages_from_history(agent: AgentService) -> list[dict]:
    """Project kani history into display messages (text only, v1)."""
    messages: list[dict] = []
    if agent.kani is None:
        return messages
    for message in agent.kani.chat_history:
        role = str(getattr(message, "role", "")).lower()
        text = str(getattr(message, "text", "") or "")
        if not text:
            continue
        if "user" in role:
            messages.append({"role": "user", "content": text})
        elif "function" in role:
            messages.append(
                {
                    "role": "tool",
                    "name": str(getattr(message, "name", "tool")),
                    "content": text[:6000],
                    "is_error": bool(getattr(message, "is_tool_call_error", False)),
                },
            )
        else:
            messages.append({"role": "assistant", "content": text})
    return messages


def delete_conversation(conversation_id: str) -> bool:
    path = conversation_path(conversation_id)
    ok = True
    try:
        path.unlink()
    except OSError as exc:
        LOG.warning("conversation delete failed: %s", exc)
        ok = False
    return ok


def clear_all() -> int:
    """Delete every conversation; returns how many deletions failed.

    Pure IO: the caller refreshes state.conversations in the Flet context.
    """
    directory = storage.conversations_dir()
    failures = 0
    for path in directory.glob("*.json"):
        try:
            path.unlink()
        except OSError as exc:
            LOG.warning("conversation delete failed: %s", exc)
            failures += 1
    LOG.info("all conversations cleared (%d failures)", failures)
    return failures


def export_conversation_markdown(conversation_id: str) -> tuple[str, str]:
    """Export conversation transcript to GitHub-flavored Markdown.

    Returns (markdown_text, suggested_filename).
    """
    path = conversation_path(conversation_id)
    if not path.exists():
        return "", "conversation.md"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return "", "conversation.md"
    if not isinstance(data, dict):
        return "", "conversation.md"
    chat_history = data.get("chat_history")
    if not isinstance(chat_history, list):
        chat_history = []

    title = _title_from_file(path)
    slug = _slugify(title) or "conversation"
    filename = f"{slug}.md"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {title}",
        "",
        f"_Exported from LM Router on {timestamp}_",
        "",
        "---",
        "",
    ]

    for msg in chat_history:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        content = msg.get("content") or msg.get("text") or ""
        if not content:
            continue
        if "user" in role:
            lines.extend(["### User", "", str(content).strip(), ""])
        elif "function" in role:
            name = msg.get("name", "tool")
            lines.extend([f"#### Tool ({name})", "", "```", str(content)[:2000].strip(), "```", ""])
        else:
            lines.extend(["### Assistant", "", str(content).strip(), ""])

    return "\n".join(lines), filename


def export_conversation_json(conversation_id: str) -> tuple[str, str]:
    """Export conversation raw data to JSON.

    Returns (json_text, suggested_filename).
    """
    path = conversation_path(conversation_id)
    if not path.exists():
        return "{}", "conversation.json"

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "{}", "conversation.json"

    title = _title_from_file(path)
    slug = _slugify(title) or "conversation"
    filename = f"{slug}.json"
    return text, filename
