"""In-memory log ring with secret redaction.

Stdlib `re` only: every pattern is linear (no nested quantifiers), so no
ReDoS risk and no regex timeout needed. ANSI/OSC escape sequences are
stripped so tool output cannot hijack the Server screen.
"""

import logging
import re
import time
from collections import deque

from . import constants

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bearer\s+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "sk-[REDACTED]"),
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
            message = redact(record.getMessage())
        except Exception:
            message = "<unformattable log record>"
        _ring.append(
            {
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "msg": message,
            }
        )
        if on_record is not None:
            try:
                on_record()
            except Exception as exc:  # callback errors must never break logging
                _ = exc


def get_logger(name: str) -> logging.Logger:
    global _handler_installed
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not _handler_installed:
        logger.addHandler(RingHandler())
        _handler_installed = True
    return logger


def records() -> list[dict]:
    """Snapshot of the ring for UI rendering."""
    return list(_ring)


def clear() -> None:
    _ring.clear()


LOG = get_logger("lmrouter")
