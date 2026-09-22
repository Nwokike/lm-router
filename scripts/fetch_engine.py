"""Refresh the bundled gateway engine from router.kiri.ng.

Usage:
    uv run python scripts/fetch_engine.py

Downloads https://router.kiri.ng/run.py, verifies it parses and carries the
expected VERSION token, writes src/assets/engine/run.py, and records
engine_version + engine_sha256 in version.json.
"""

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ENGINE_URL = "https://router.kiri.ng/run.py"
ROOT = Path(__file__).resolve().parent.parent
ENGINE_PATH = ROOT / "src" / "assets" / "engine" / "run.py"
VERSION_PATH = ROOT / "version.json"


def main() -> int:
    print(f"fetching {ENGINE_URL} ...")
    with urllib.request.urlopen(ENGINE_URL, timeout=30) as resp:
        data = resp.read()

    if b"if __name__ ==" not in data:
        print("error: downloaded file is not the expected gateway script")
        return 1

    match = re.search(rb'VERSION = "([^"]+)"', data)
    if not match:
        print("error: VERSION token missing in downloaded script")
        return 1
    engine_version = match.group(1).decode()
    engine_sha = hashlib.sha256(data).hexdigest()

    ENGINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGINE_PATH.write_bytes(data)
    print(f"wrote {ENGINE_PATH.relative_to(ROOT)} ({len(data)} bytes)")
    print(f"engine_version: {engine_version}")
    print(f"engine_sha256:  {engine_sha}")

    version = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    version["engine_version"] = engine_version
    version["engine_sha256"] = engine_sha
    VERSION_PATH.write_text(
        json.dumps(version, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("updated version.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
