"""Version and engine-pin consistency across pyproject, version.json, constants."""

import json
import tomllib
from pathlib import Path

from core.constants import APP_VERSION, BUILD_NUMBER

ROOT = Path(__file__).resolve().parents[1]


def _load() -> tuple[dict, dict]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    return pyproject, version


def test_app_version_matches_everywhere() -> None:
    pyproject, version = _load()
    assert pyproject["project"]["version"] == APP_VERSION
    assert version["version"] == APP_VERSION
    # Deliberately not pinned to a literal: bumping the release must not mean
    # editing a test. The three sources must simply agree.
    assert APP_VERSION.count(".") == 2
    assert APP_VERSION.startswith("1."), f"expected the 1.x line, got {APP_VERSION}"


def test_build_number_matches() -> None:
    pyproject, version = _load()
    assert pyproject["tool"]["flet"]["build_number"] == version["build_number"]
    assert version["build_number"] == BUILD_NUMBER


def test_no_engine_is_vendored_in_the_repo() -> None:
    """The gateway is fetched live; nothing about it is pinned in the app.

    router.kiri.ng changes constantly, so a vendored copy would silently
    shadow it (that is exactly how a stale 1463-line engine without the
    `auto` model, rate hints or /account-limits ended up shipping).
    """
    assert not (ROOT / "src" / "assets" / "engine").exists()
    _, version = _load()
    # The pin keys are gone with the file they described.
    assert "engine_sha256" not in version
    assert "engine_version" not in version


def test_interpreter_band_is_pinned() -> None:
    pyproject, _ = _load()
    requires = pyproject["project"]["requires-python"]
    # A python-build manifest bump must never silently jump to 3.15.
    assert "<3.15" in requires


def test_target_arch_not_restricted() -> None:
    pyproject, _ = _load()
    # No ABI restriction: CI builds --split-per-abi, every supported ABI
    # ships as its own APK. Do not reintroduce target_arch without a
    # demonstrated incompatibility on that architecture.
    android = pyproject["tool"]["flet"]["android"]
    assert "target_arch" not in android


def test_dead_boot_screen_key_stays_removed() -> None:
    pyproject, _ = _load()
    # flet-cli 1.0.0 never reads tool.flet.app.boot_screen — do not restore.
    app_table = pyproject["tool"]["flet"]["app"]
    assert "boot_screen" not in app_table


def test_playstore_url_matches_the_android_bundle_id() -> None:
    """A Play button pointing at the wrong package is a dead link.

    The URL must carry exactly the bundle_id, or the update dialog sends users
    to a different app (or a 404).
    """
    import tomllib

    from core.constants import PLAYSTORE_URL

    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    bundle_id = pyproject["tool"]["flet"]["android"]["bundle_id"]

    assert PLAYSTORE_URL, "PLAYSTORE_URL is empty; the update dialog's Play button is dead"
    assert f"id={bundle_id}" in PLAYSTORE_URL, (
        f"PLAYSTORE_URL must contain id={bundle_id}, got {PLAYSTORE_URL}"
    )
    assert PLAYSTORE_URL.startswith("https://play.google.com/store/apps/details")


def test_relaxed_admob_guard_is_flagged_for_restore() -> None:
    """The AdMob guard is relaxed on purpose; keep the reminder discoverable.

    We ship Google's TEST ad units until the Play listing exists. A future
    release must swap in production IDs and restore the hard-fail branch — the
    warnings in pyproject.toml and the workflow exist to make that hard to miss.
    """
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow_text = (ROOT / ".github" / "workflows" / "build-all.yml").read_text(encoding="utf-8")
    constants_text = (ROOT / "src" / "core" / "constants.py").read_text(encoding="utf-8")

    # The test ID really is still in place (so the guard has something to catch).
    assert "3940256099942544" in pyproject_text
    assert "3940256099942544" in constants_text
    # Both places carry an explicit restore reminder.
    assert "PRODUCTION" in pyproject_text.upper()
    assert "RESTORE" in workflow_text.upper()
