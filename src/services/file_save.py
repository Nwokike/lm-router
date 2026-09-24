"""Save a generated file to disk, with a real dialog and a fallback.

"Export" used to copy the markdown to the clipboard, which is not an export —
it loses the file the moment the clipboard is reused. This asks Flet's
FilePicker for a destination, and falls back to the platform Downloads folder
when there is no dialog (headless, or a platform without one), so an export
always produces a file.

Pure stdlib plus Flet: no new dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

import flet as ft

from core.logging import LOG


def _is_mobile(page: object | None) -> bool:
    platform = getattr(page, "platform", None)
    if platform is None:
        return False
    try:
        return bool(platform.is_mobile())
    except Exception:
        return False


def _downloads_dir(page: object | None) -> Path:
    """Where to put the file when no dialog is available."""
    if _is_mobile(page):
        android = Path("/storage/emulated/0/Download")
        if android.exists():
            return android
    return Path(os.path.expanduser("~")) / "Downloads"


def _unique_path(directory: Path, filename: str) -> Path:
    """Never silently overwrite: `name (2).md`."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    counter = 2
    while (directory / f"{stem} ({counter}){suffix}").exists():
        counter += 1
    return directory / f"{stem} ({counter}){suffix}"


async def save_text_file(
    page: ft.Page,
    content: str,
    filename: str,
    dialog_title: str | None = None,
) -> str | None:
    """Write `content` to a user-chosen path. Returns the path, or None.

    Never raises: a cancelled dialog or a read-only destination returns None so
    the caller can show one honest message instead of a traceback.
    """
    picker = getattr(page, "file_picker", None)
    if picker is None:
        picker = ft.FilePicker()
        page.services.append(picker)
        try:
            page.file_picker = picker  # a flet attribute, not a typo
        except Exception as exc:
            LOG.debug("could not attach file_picker to page: %s", exc)
        try:
            page.update()
        except Exception as exc:
            LOG.debug("file picker registration update failed: %s", exc)

    destination: str | None = None
    try:
        destination = await picker.save_file(
            dialog_title=dialog_title or f"Save {filename}",
            file_name=filename,
        )
    except Exception as exc:
        # Cancelled dialogs and unsupported platforms both land here.
        LOG.debug("save_file unavailable (%s); using Downloads", exc)

    try:
        if destination:
            path = Path(destination)
        else:
            directory = _downloads_dir(page)
            directory.mkdir(parents=True, exist_ok=True)
            path = _unique_path(directory, filename)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        LOG.warning("could not write %s: %s", filename, exc)
        return None
    return str(path)
