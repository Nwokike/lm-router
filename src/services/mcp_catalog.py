"""Curated MCP catalog: one-tap add for people who will never write JSON.

Research + live handshakes (2026-09-27): every URL below completed a real
initialize/tools/list round without credentials (keyless) or with the
documented auth header (keyed). An AI chat tool is only as good as its
tools; this list is the zero-setup path for users who do not know what an
MCP server is. Descriptions are written for people, not developers.

v1 is deliberately header-auth only: servers behind OAuth (Notion, Figma,
Linear, Sentry...) need a real sign-in flow and stay out until we build one.
"""

# Stable id of the protected built-in entry. Seeded on boot when missing;
# never duplicated, never re-added, never deletable (only disablable).
BUILTIN_EXA_ID = "builtin-exa"
BUILTIN_EXA_URL = "https://mcp.exa.ai/mcp"

PRESETS: tuple[dict, ...] = (
    # ---- keyless: one tap and it connects ----
    {
        "id": "exa",
        "name": "Exa",
        "url": BUILTIN_EXA_URL,
        "auth": None,
        "builtin": True,
        "description": (
            "Searches the web and reads pages for you, so answers come with "
            "real sources instead of guesses."
        ),
    },
    {
        "id": "deepwiki",
        "name": "DeepWiki",
        "url": "https://mcp.deepwiki.com/mcp",
        "auth": None,
        "description": (
            "Explains how any open-source project works, like a guide who "
            "already read the whole codebase."
        ),
    },
    {
        "id": "context7",
        "name": "Context7",
        "url": "https://mcp.context7.com/mcp",
        "auth": None,
        "description": (
            "Looks up the latest instructions for any software tool, so setup "
            "steps are never out of date."
        ),
    },
    {
        "id": "huggingface",
        "name": "Hugging Face",
        "url": "https://huggingface.co/mcp",
        "auth": None,
        "description": "Finds open AI models and datasets you can browse, download and try.",
    },
    {
        "id": "openai-docs",
        "name": "OpenAI Docs",
        "url": "https://developers.openai.com/mcp",
        "auth": None,
        "description": "Looks up official OpenAI documentation and API details.",
    },
    {
        "id": "claude-docs",
        "name": "Claude Docs",
        "url": "https://platform.claude.com/docs/mcp",
        "auth": None,
        "description": "Looks up official Claude documentation and API details.",
    },
    {
        "id": "cloudflare-docs",
        "name": "Cloudflare Docs",
        "url": "https://docs.mcp.cloudflare.com/mcp",
        "auth": None,
        "description": "Answers questions about building and running websites on Cloudflare.",
    },
    {
        "id": "roundtable",
        "name": "Roundtable",
        "url": "https://mcp.roundtable.now/mcp",
        "auth": None,
        "description": (
            "Asks several AI models the same question and shows you where they agree and disagree."
        ),
    },
    # ---- key-required: the app collects the key and wires the header ----
    {
        "id": "github",
        "name": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth": {
            "key_label": "Personal access token",
            "get_key_url": "https://github.com/settings/personal-access-tokens/new",
            "hint": (
                "A read-only fine-grained token is enough; GitHub hides "
                "any tool your token cannot reach."
            ),
        },
        "description": (
            "Connects to your GitHub so the assistant can read code, issues and pull requests."
        ),
    },
    {
        "id": "perplexity",
        "name": "Perplexity",
        "url": "https://api.perplexity.ai/mcp",
        "auth": {
            "key_label": "API key",
            "get_key_url": "https://docs.perplexity.ai",
            "hint": "Deep, sourced research answers with citations.",
        },
        "description": "Deep, sourced research answers to any question, with citations.",
    },
    {
        "id": "tavily",
        "name": "Tavily",
        "url": "https://mcp.tavily.com/mcp",
        "auth": {
            "key_label": "API key",
            "get_key_url": "https://app.tavily.com",
            "hint": "Web search tuned for AI, so research comes back fast and clean.",
        },
        "description": "Searches the web tuned for AI, so research comes back fast and clean.",
    },
    {
        "id": "firecrawl",
        "name": "Firecrawl",
        "url": "https://mcp.firecrawl.dev/v2/mcp",
        "auth": {
            "key_label": "API key",
            "get_key_url": "https://www.firecrawl.dev/app/api-keys",
            "hint": "Reads and crawls websites, including pages that need JavaScript.",
        },
        "description": (
            "Reads and crawls websites, including pages that need JavaScript, "
            "and pulls out the text."
        ),
    },
    {
        "id": "apify",
        "name": "Apify",
        "url": "https://mcp.apify.com",
        "auth": {
            "key_label": "API token",
            "get_key_url": "https://console.apify.com/account/integrations",
            "hint": "Thousands of web-scraping and automation tools on demand.",
        },
        "description": "Runs thousands of web-scraping and automation tools on demand.",
    },
)


def preset_payload(preset: dict, api_key: str = "") -> dict:
    """Config dict for `methods.add_mcp_server`.

    Keyless presets never send headers; a supplied key becomes the standard
    Authorization bearer header (GitHub's remote endpoint reads exactly
    this). The caller enforces that a keyed preset has a key."""
    headers: dict[str, str] = {}
    key = str(api_key or "").strip()
    if preset.get("auth") and key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict = {
        "name": str(preset["name"]),
        "transport": "streamable_http",
        "url": str(preset["url"]),
        "headers": headers,
    }
    if preset.get("builtin"):
        payload["id"] = BUILTIN_EXA_ID
        payload["protected"] = True
    return payload
