"""Version and engine-pin consistency across pyproject, version.json, constants."""

import hashlib
import json
import tomllib
from pathlib import Path

from core.constants import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _load() -> tuple[dict, dict]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    return pyproject, version


def test_app_version_matches_everywhere() -> None:
    pyproject, version = _load()
    assert pyproject["project"]["version"] == APP_VERSION
    assert version["version"] == APP_VERSION
    assert APP_VERSION == "0.1.0"


def test_build_number_matches() -> None:
    pyproject, version = _load()
    assert pyproject["tool"]["flet"]["build_number"] == version["build_number"]


def test_engine_pin_matches_bundled_file() -> None:
    _, version = _load()
    engine = (ROOT / "src" / "assets" / "engine" / "run.py").read_bytes()
    assert hashlib.sha256(engine).hexdigest() == version["engine_sha256"]
    assert f'VERSION = "{version["engine_version"]}"'.encode() in engine
    # import safety: the engine must keep its main guard
    assert b'if __name__ == "__main__":' in engine
