"""Tunnel helpers: the bypass-header injection and teardown safety."""

from __future__ import annotations

from services.tunnel import LocalTunnel, _augment_head


def test_augment_head_inserts_the_bypass_header_once() -> None:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json"
    sep = b"\r\n\r\n"
    out = _augment_head(head, sep)
    assert out.startswith(b"HTTP/1.1 200 OK")
    assert b"bypass-tunnel-reminder: true" in out
    assert out.endswith(sep), "the blank line must stay the terminator"


def test_augment_head_is_idempotent_and_passes_non_http() -> None:
    head = b"HTTP/1.1 200 OK\r\nbypass-tunnel-reminder: true"
    sep = b"\r\n\r\n"
    assert _augment_head(head, sep) == head + sep, "already present: byte-identical"

    binary = b"\x89PNG\r\n\x1a\n"
    assert _augment_head(binary, sep) == binary + sep, "non-HTTP passes through"


def test_stop_is_safe_on_a_bare_instance() -> None:
    tunnel = LocalTunnel(8082)
    tunnel.stop()  # no claim, no threads: must never raise
    assert tunnel.public_url == ""
