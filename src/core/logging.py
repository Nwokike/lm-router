"""In-memory log ring with secret redaction.

Stdlib `re` only: every pattern is linear (no nested quantifiers), so no
ReDoS risk and no regex timeout needed. ANSI/OSC escape sequences are
stripped so tool output cannot hijack the Server screen.
"""

import logging
import logging.handlers
import re
import time
from collections import deque
from pathlib import Path

from . import constants, storage

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bearer\s+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-[REDACTED]"),  # sk-proj-/sk-ant- include "-"
    (re.compile(r"xox[bpas]-[A-Za-z0-9-]+"), "[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (
        re.compile(
            r"(authorization|x-api-key|api[_-]?key|token)\s*[=:]\s*[\"']?[^\s\"']+",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
]

_ring: deque[dict] = deque(maxlen=constants.LOG_RING_SIZE)
_handler_installed = False
on_record = None  # optional zero-arg callback (set by AppController to bump UI state)


def redact(text: str) -> str:
    text = text[:2000]
    text = ANSI_RE.sub("", text)
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Ad delivery is operator business (owner rule): ad_service
            # records never surface in the on-screen ring/terminal. The file
            # and console handlers keep them for real debugging.
            if "ad_service" in (record.pathname or ""):
                return
            message = redact(record.getMessage())
        except Exception:
            message = "<unformattable log record>"
        _ring.append(
            {
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "msg": message,
            },
        )
        if on_record is not None:
            try:
                on_record()
            except Exception as exc:  # callback errors must never break logging
                _ = exc


def log_path() -> Path:
    return storage.cache_dir() / "lm-router.log"


class RedactFileHandler(logging.handlers.RotatingFileHandler):
    """Always-on rotating file log — GUI users may never see the terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Format FIRST (Formatter needs a LogRecord), redact the text after.
            self.stream.write(redact(self.format(record)) + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


class RedactStreamHandler(logging.StreamHandler):
    """Console output with the same redaction the file log applies."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(redact(self.format(record)) + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def tail(lines: int = 200) -> str:
    try:
        with log_path().open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return "(no log file yet)"


def get_logger(name: str) -> logging.Logger:
    global _handler_installed
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not _handler_installed:
        logger.addHandler(RingHandler())
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        # `uv run flet run` printed nothing at all: every handler wrote to a
        # ring buffer or a file, so the terminal was the one place with no
        # output. Redaction is identical, so a key can never reach the
        # console that it is kept out of the file.
        console_handler = RedactStreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        try:
            file_handler = RedactFileHandler(
                log_path(),
                maxBytes=512_000,
                backupCount=2,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            _ring.append(
                {
                    "ts": time.strftime("%H:%M:%S"),
                    "level": "WARNING",
                    "msg": f"file log unavailable: {exc}",
                },
            )
        _handler_installed = True
    return logger


def records() -> list[dict]:
    """Snapshot of the ring for UI rendering."""
    return list(_ring)


def clear() -> None:
    _ring.clear()


LOG = get_logger("lmrouter")
