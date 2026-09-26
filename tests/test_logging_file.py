"""File log: always-on, redacted, and tail()-able (GUI users cannot see stdout).

The RedactFileHandler bug (formatting a string instead of a LogRecord) made
the log file 0 bytes and silently disabled the Copy-logs escape hatch — these
tests lock the handler contract down.
"""

import logging

from core import storage
from core.logging import RedactFileHandler, log_path, redact, tail


def test_file_handler_writes_redacted(tmp_path) -> None:
    path = tmp_path / "test.log"
    handler = RedactFileHandler(path, maxBytes=100_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    record = logging.LogRecord(
        name="lmrouter",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token sk-abcdefgh12345678 rejected; Authorization: Bearer abc.def.ghi",
        args=None,
        exc_info=None,
    )
    handler.emit(record)
    handler.close()

    text = path.read_text(encoding="utf-8")
    # No secret material survives (bearer + sk- rules may both fire)
    assert "sk-abcdefgh12345678" not in text
    assert "abc.def.ghi" not in text
    assert "sk-[REDACTED]" in text
    assert text.count("[REDACTED]") >= 2
    assert "INFO" in text


def test_redact_strips_ansi_and_keys() -> None:
    out = redact("\x1b[31mred\x1b[0m sk-1234567890abcdef")
    assert "\x1b" not in out
    assert "sk-[REDACTED]" in out


def test_log_path_and_tail(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.logging.storage.base_dir", lambda: tmp_path)
    assert log_path() == storage.cache_dir() / "lm-router.log"
    assert "(no log file yet)" in tail()

    log_path().write_text("line one\nline two\nline three\n", encoding="utf-8")
    out = tail(2)
    assert "line two" in out
    assert "line one" not in out


def test_redact_covers_modern_key_shapes() -> None:
    """sk-proj- and sk-ant- contain a hyphen; the old [A-Za-z0-9] class
    stopped after "sk-" and let today's default keys reach file, stdout and
    the on-screen ring unredacted."""
    from core.logging import redact

    for key in (
        "sk-proj-abcdefgh12345678ABCD",
        "sk-ant-api03-abcdefghijklmnop",
        "sk-t01-abcdefghijklmnopqrst",
    ):
        line = f"request failed with {key} in the header"
        cleaned = redact(line)
        assert key not in cleaned, f"{key} survived redaction: {cleaned}"
        assert "sk-[REDACTED]" in cleaned


def test_ad_service_records_never_reach_the_screen_ring() -> None:
    """Ad delivery is operator business (owner rule): ad_service records may
    land in the file/console for debugging but must never surface in the
    on-screen log ring the Server screen renders."""
    import logging

    from core import logging as applog

    ring_before = len(applog.records())
    ad_record = logging.LogRecord(
        "lmrouter",
        logging.INFO,
        r"C:\app\src\services\ad_service.py",
        58,
        "ads: non-mobile platform, consent flow skipped",
        None,
        None,
    )
    applog.RingHandler().emit(ad_record)
    assert len(applog.records()) == ring_before, "ad record must not enter the ring"
    assert not any("ads:" in record.get("msg", "") for record in applog.records()), (
        "an ad record is visible in the ring"
    )

    # Ordinary app records still flow.
    normal = logging.LogRecord(
        "lmrouter",
        logging.INFO,
        r"C:\app\src\screens\server_screen.py",
        1,
        "ring-visible-marker",
        None,
        None,
    )
    applog.RingHandler().emit(normal)
    assert any("ring-visible-marker" in record.get("msg", "") for record in applog.records())
