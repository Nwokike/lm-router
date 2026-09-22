"""Fernet encryption for secrets at rest.

cryptography ships with the app already (via the mcp dependency), so provider
API keys are encrypted with a locally generated master key stored next to the
settings file. Single-user local app: the key protects against casual file
copying, not an attacker with full disk access.
"""

from cryptography.fernet import Fernet, InvalidToken

from . import storage
from .logging import LOG

_key: Fernet | None = None


def _load_key() -> Fernet:
    global _key
    if _key is not None:
        return _key
    path = storage.master_key_path()
    try:
        key = path.read_bytes()
    except OSError:
        key = Fernet.generate_key()
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            LOG.warning("could not restrict permissions on %s", path)
    _key = Fernet(key)
    return _key


def encrypt(plaintext: str) -> str:
    return _load_key().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _load_key().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("stored secret is unreadable (key file changed?)") from exc
