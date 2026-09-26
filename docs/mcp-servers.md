# MCP servers in LM Router

Research + verified configurations for the Connect-a-tool feature
(Settings > MCP). Everything below was probed through LM Router's own
hub (`hub.test()`), not copied from a README, on 2026-09-26.

## What the app exposes

The SDK (python `mcp` package) defines exactly these knobs, and the
add form exposes all of them. The UI is never the limiting layer:

| Transport | Fields |
|---|---|
| `streamable_http` (default) | URL, Headers JSON, Request timeout (s), SSE read timeout (s) |
| `sse` | URL, Headers JSON, Request timeout (s), SSE read timeout (s) |
| `stdio` | Command line (command + arguments), Headers JSON, Environment JSON, Working directory |

Plus after connecting: per-server and per-tool enable/disable from the
MCP tools dialog.

Notes on the fields:

- **Command line**: paste the one line every MCP README gives you, e.g.
  `npx -y @modelcontextprotocol/server-everything`. The app splits it into
  command + arguments (quoted paths with spaces are kept intact; Windows
  backslashes are never eaten).
- **Environment JSON** (stdio): extra variables for the server, e.g.
  `{"API_KEY": "..."}`. Merged on top of the SDK's safe inherited
  whitelist (PATH, TEMP, HOME, ...), so the launcher still resolves while
  the server gets what it asked for. Values live only in this device's
  settings file, like any other MCP client's local config.
- **Timeouts** (remote): default request timeout is 30s (streamable) or
  5s (sse); SSE read timeout defaults to 300s. Raise them for slow
  servers; must be positive.
- **Headers** are HTTP-only auth. They are never converted into
  environment variables for stdio children (an Authorization header once
  leaked into a child process environment; that path is closed).

## Verified remote servers (Streamable HTTP)

| Server | URL | Tools seen | Notes |
|---|---|---|---|
| Exa | `https://mcp.exa.ai/mcp` | 2 (`web_search_exa`, `web_fetch_exa`) | keyless for light use |
| Context7 | `https://mcp.context7.com/mcp` | 2 (`resolve-library-id`, `query-docs`) | library docs lookup |
| DeepWiki | `https://mcp.deepwiki.com/mcp` | 3 (`read_wiki_structure`, `read_wiki_contents`, `ask_wiki_question`) | keyless |

`mcp.duckduckgo.com` and `huggingface.co/mcp` did not answer from the
test network; both fail loudly (`Server unreachable. Check the URL or
command.`) rather than hanging. Verify an endpoint from your own network
before recommending it.

If a remote server needs a key, put it in Headers JSON, e.g.
`{"Authorization": "Bearer ..."}`.

## Verified local servers (stdio)

| Command line | Requires | Tools seen |
|---|---|---|
| `npx -y @modelcontextprotocol/server-everything` | Node.js | 13 |
| `uvx mcp-server-fetch` | uv (Python) | 1 |

Windows notes:

- Type `npx`, not `npx.CMD`: the app resolves the launcher through PATH
  before spawning. CreateProcess applies no PATHEXT, so spawning the
  typed name would fail with WinError 2 even though Node is installed.
- `uvx` takes no `-y` flag (that is an npx convention).

## Android: remote only

The APK ships its own Python. There is no Node, no uv, no npm, and no
subprocess model for stdio, so:

- stdio servers are refused up front with
  `stdio transport is not supported on mobile devices. Use streamable_http or sse.`
- remote servers work exactly as on desktop.

## Is the app's Python sandboxed?

No. Desktop builds run a bundled Python interpreter (the packaged
environment, or `.venv` in development), and it is not sandboxed: stdio
children run as ordinary processes with your user account's rights. The
guards are configuration-level, not OS-level:

- only servers the user explicitly adds are ever spawned;
- remote URLs must be `http://` or `https://`;
- a missing launcher fails fast with install instructions instead of a
  raw ENOENT;
- a failed child is asked for its stderr so the error names the fix;
- connects are bounded (20s) and one dead server never takes down the
  others.

Only add stdio servers you would be willing to run as a program on your
machine, because that is what you are doing.

## Error messages and what they mean

| Message | Meaning | What to do |
|---|---|---|
| `` `uvx` was not found on this device ...`` | launcher not installed / not on PATH | install uv or Node, or add a remote server |
| `Connection closed. Server output: ...` | the server process died at startup; its own stderr is appended | follow the appended output (wrong flag, missing script, crash) |
| `timed out (check your connection)` | no handshake within 20s | check network/URL, or raise nothing: retry |
| `Server unreachable. Check the URL or command.` | DNS failure or connection refused | fix the hostname/URL (or the command) |
| `Authentication failed. Check the headers or key.` | 401/403 | fix Headers JSON |
| `Server speaks an incompatible MCP protocol version.` | server predates the protocol the SDK speaks | update the server |
| `stdio transport is not supported on mobile ...` | stdio on Android | use Streamable HTTP |
| `Quotes are unbalanced in your command.` / `Enter a command, ...` | form validation | fix the command line |

The per-row **Test** button runs this exact path before you rely on a
server: connect, list tools, validate each tool's JSON schema, and show
the row as ready or the reason it is not.

## Trust rules

- MCP tool results (file contents, web pages, server messages) are
  untrusted DATA: the assistant is instructed to quote or summarize them
  and never follow instructions found inside them.
- Tool descriptions are capped at 4000 characters before they reach the
  model (token-bomb and prompt-injection budget).
- Bulk actions (retest-all, refresh) are deliberately NOT exposed as AI
  tools; they would be an injection-budget multiplier.
- Only the servers and tools you enable are ever connected.

## Landscape (why Streamable HTTP first)

- Industry survey (Zuplo, Dec 2025): Streamable HTTP is now the majority
  transport (roughly 59% of servers vs 34% stdio).
- The MCP spec deprecated SSE in its 2025-06-18 revision in favor of
  Streamable HTTP; the app still supports SSE for older servers.
- Reference lists for discovery:
  `github.com/modelcontextprotocol/servers` (official examples),
  `github.com/wong2/awesome-mcp-servers`,
  `github.com/sylviangth/awesome-remote-mcp-servers`.
