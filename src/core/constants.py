"""Application-wide constants (single source: APP_VERSION)."""

APP_NAME = "LM Router"
APP_VERSION = "1.0.0"  # synced with pyproject + version.json by tests/test_version_sync.py
BUILD_NUMBER = 1  # synced with version.json build_number

ENGINE_URL = "https://router.kiri.ng/run.py"
GITHUB_RELEASE_URL = "https://github.com/Nwokike/lm-router/releases/latest"
UPDATE_CONFIG_URL = "https://raw.githubusercontent.com/Nwokike/lm-router/main/version.json"
# Must match [tool.flet.android] bundle_id (ng.kiri.lmrouter) or the Play
# button lands on the wrong/nonexistent listing.
PLAYSTORE_URL = "https://play.google.com/store/apps/details?id=ng.kiri.lmrouter"
DEFAULT_GATEWAY_PORT = 8082

SETTINGS_FILE = "app_settings.json"
CONVERSATIONS_DIR = "conversations"

LOG_RING_SIZE = 500
INTERSTITIAL_EVERY = 3

# Production AdMob units (from the AdMob console, 2026-09-25).
# USE_TEST_IDS stays False; CI fails any build whose files carry the Google
# test publisher prefix, so a test unit can never ship by accident.
USE_TEST_IDS = False
AD_BANNER_UNIT_ID_ANDROID = "ca-app-pub-5679949845754640/9070470494"
AD_INTERSTITIAL_UNIT_ID_ANDROID = "ca-app-pub-5679949845754640/2372451777"

# Keyless hosted search endpoint (same one OpenCode's own websearch tool calls).
SEARCH_ENDPOINT = "https://mcp.exa.ai/mcp"
SEARCH_TOOL_NAME = "web_search_exa"
SEARCH_TIMEOUT = 25.0
