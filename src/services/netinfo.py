"""Local network discovery for the gateway's status readout.

The gateway binds 127.0.0.1 only, so it is not reachable from other devices
and a QR code would be a lie. This module therefore only reports the host's
own primary interface, for display in the Server tab. Pure stdlib.
"""

from __future__ import annotations

import socket


def get_lan_ip() -> str:
    """Best-effort discovery of the primary local IP, without sending packets.

    Connecting a UDP socket to an unrouted address makes the OS pick the
    outgoing interface without transmitting anything.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
