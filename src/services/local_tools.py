"""Desktop-local tools: shell commands and file reads.

Real implementations (not placeholders) of the `shell` and `read` tools the
model kept inventing. Both follow the house AIFunction pattern (Annotated /
AIParam, docstring-as-desc, auto_retry=False) and never raise — failures
become error strings so a bad path or hung command costs the turn a message,
not a crash.

Desktop-only by registration (main.py gates on is_mobile, same as stdio
MCP): phones have no shell and no shared filesystem worth reading.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Annotated

from kani import AIParam
from kani.ai_function import AIFunction

SHELL_TIMEOUT_S = 30
SHELL_OUTPUT_CAP = 8000
READ_SIZE_CAP = 100 * 1024


def build_shell_tool() -> AIFunction:
    async def shell(
        command: Annotated[str, AIParam("Shell command to run on the user's machine.")],
        cwd: Annotated[str, AIParam("Working directory. Omit for the app's.")] = "",
    ) -> str:
        """Run a shell command on the user's machine and return its output.

        Use only for commands the user explicitly asked for. Never run
        anything destructive (delete, overwrite, format, package-remove)
        without the user confirming it first. Long-running servers and
        interactive programs will time out — prefer one-shot commands.
        """
        if not str(command or "").strip():
            return "No command given."
        try:
            proc = await asyncio.to_thread(  # noqa: S604 (user-asked commands need a shell)
                subprocess.run,
                str(command),
                shell=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT_S,
                cwd=str(cwd).strip() or None,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {SHELL_TIMEOUT_S}s: {command[:120]}"
        except Exception as exc:
            return f"Command failed to start: {exc}"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > SHELL_OUTPUT_CAP:
            out = out[:SHELL_OUTPUT_CAP] + "\n…(output truncated)"
        return f"exit={proc.returncode}\n{out}".strip() or "Command produced no output."

    return AIFunction(shell, name="shell", auto_retry=False, auto_truncate=6000)


def build_read_tool() -> AIFunction:
    async def read(
        path: Annotated[str, AIParam("File path to read, absolute or ~/relative.")],
    ) -> str:
        """Read a text file from the user's machine and return its content.

        Use for files the user points at (logs, configs, notes). Binary
        files and directories cannot be read — say so instead of guessing.
        """
        raw = str(path or "").strip()
        if not raw:
            return "No path given."
        try:
            resolved = Path(raw).expanduser()
            if resolved.is_dir():
                return f"Not a file (is a directory): {resolved}"
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"File not found: {raw}"
        except OSError as exc:
            return f"Cannot read {raw}: {exc}"
        if len(text) > READ_SIZE_CAP:
            text = text[:READ_SIZE_CAP] + "\n…(file truncated)"
        lines = text.count("\n") + 1
        return f"{resolved} ({lines} lines):\n{text}"

    return AIFunction(read, name="read", auto_retry=False, auto_truncate=6000)
