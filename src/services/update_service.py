"""Update check: fetch remote version.json, return it when newer (FFmpeg port).

The repo is private during development, so the raw fetch fails silently
until the repo is public: that is the designed graceful no-op.
"""

from typing import Any

import httpx

from core import constants
from core.logging import LOG


class UpdateService:
    """Checks the repository version.json manifest in the background."""

    @staticmethod
    async def check_for_updates() -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
                resp = await client.get(constants.UPDATE_CONFIG_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    remote_build = int(data.get("build_number", 0))
                    if remote_build > constants.BUILD_NUMBER:
                        LOG.info(
                            "update available: build %s > %s",
                            remote_build,
                            constants.BUILD_NUMBER,
                        )
                        return data
        except Exception as exc:
            LOG.info("silent update check skipped: %s", exc)
        return None
