"""Conversation persistence: kani save/load files under conversations/."""

import json
import time
import uuid
from pathlib import Path

from core import storage
from core.logging import LOG
from core.state import state
from services.agent import AgentService


def _title_from_file(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return path.stem
    for message in data.get("chat_history", []):
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
        except OSError:
            continue
        items.append(
            {
                "id": path.stem,
                "title": _title_from_file(path),
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                "path": str(path),
            }
        )
    items.sort(key=lambda item: item["updated"], reverse=True)
    return items


def start_conversation() -> str:
    state.active_conversation = uuid.uuid4().hex[:12]
    return state.active_conversation


def conversation_path(conversation_id: str) -> Path:
    return storage.conversations_dir() / f"{conversation_id}.json"


def save_conversation(agent: AgentService) -> None:
    if not state.active_conversation or agent.kani is None:
        return
    try:
        agent.kani.save(str(conversation_path(state.active_conversation)))
    except Exception as exc:
        LOG.warning("conversation save failed: %s", exc)
    state.conversations = list_conversations()


def load_conversation(agent: AgentService, conversation_id: str) -> bool:
    path = conversation_path(conversation_id)
    if not path.exists() or agent.kani is None:
        return False
    try:
        agent.kani.load(str(path))
    except Exception as exc:
        LOG.warning("conversation load failed: %s", exc)
        return False
    state.active_conversation = conversation_id
    state.messages = _messages_from_history(agent)
    LOG.info("loaded conversation %s (%d messages)", conversation_id, len(state.messages))
    return True


def _messages_from_history(agent: AgentService) -> list[dict]:
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
                }
            )
        else:
            messages.append({"role": "assistant", "content": text})
    return messages


def delete_conversation(conversation_id: str) -> None:
    path = conversation_path(conversation_id)
    try:
        path.unlink()
    except OSError as exc:
        LOG.warning("conversation delete failed: %s", exc)
    state.conversations = list_conversations()


def clear_all() -> None:
    directory = storage.conversations_dir()
    for path in directory.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass
    state.conversations = []
    LOG.info("all conversations cleared")
