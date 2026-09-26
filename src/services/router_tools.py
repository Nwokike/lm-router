"""Live router facts as chat tools: status, catalog, single-model probe.

Read-mostly, local, zero-auth, cheap. Deliberately NOT exposed here: bulk
retest and `?refresh=true` sweeps stay UI buttons (tool calls are
model-driven, and a prompt-injected instruction could otherwise burn the
user's rate budget on a full-catalog sweep). Every returned string comes
from the gateway's own projection, so using these tools can never leak a
source name or gateway token.
"""

from __future__ import annotations

import contextlib
from typing import Annotated

from kani import AIParam
from kani.ai_function import AIFunction

from core.catalog import status_label
from core.state import state
from services import model_bench
from services.http import HttpService


def _root() -> str:
    """http://127.0.0.1:8082/v1 -> http://127.0.0.1:8082 (live: the port can
    change across restarts, so read it from state at call time)."""
    base = state.gateway_base_url
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _fmt_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


async def gateway_status(http: HttpService) -> str:
    """Health + counts only (the /status surface is counts-only by design)."""
    try:
        health = await http.get(f"{_root()}/health")
        payload = health.json() if health.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return "Gateway unreachable. The user can press Start gateway on the Server tab."
    except Exception:
        return "Gateway unreachable. The user can press Start gateway on the Server tab."
    parts = [f"gateway running, uptime {_fmt_uptime(int(payload.get('uptime_sec') or 0))}"]
    try:
        status = await http.get(f"{_root()}/status")
        data = status.json() if status.status_code == 200 else {}
    except Exception:
        data = {}
    if isinstance(data, dict):
        models = data.get("models") or {}
        sources = data.get("sources") or {}
        if models:
            parts.append(
                "models: "
                f"{models.get('total', 0)} total, "
                f"{models.get('active', 0)} active, "
                f"{models.get('degraded', 0)} capped or slow, "
                f"{models.get('failed', 0)} failed"
            )
        if sources:
            parts.append(
                f"sources: {sources.get('responsive', 0)}/{sources.get('total', 0)} responsive"
            )
        discovery = data.get("last_discovery_sec")
        if isinstance(discovery, int):
            parts.append(f"last catalog probe {discovery}s ago")
    return "; ".join(parts)


async def list_models(http: HttpService, status_filter: str = "") -> str:
    """The catalog in SERVER order (invariant 17: never re-sort), one line
    per model. `status_filter` matches either the raw or the display word."""
    try:
        response = await http.get(f"{_root()}/v1/models")
        if response.status_code != 200:
            return f"models request failed: HTTP {response.status_code}"
        payload = response.json()
    except Exception as exc:
        return f"models request failed: {str(exc)[:120]}"
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return "models request returned an unexpected shape."
    lines: list[str] = []
    wanted = status_filter.strip().lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "")
        if not model_id:
            continue
        raw_status = str(row.get("status") or "active")
        display = status_label(raw_status)
        if wanted and wanted not in display.lower() and wanted not in raw_status.lower():
            continue
        latency = row.get("latency_ms")
        suffix = f", {latency}ms" if isinstance(latency, int) else ""
        lines.append(f"{model_id}: {display}{suffix}")
    if not lines:
        return "No models matched that filter."
    header = f"{len(lines)} models (server order):"
    return header + "\n" + "\n".join(lines)


async def probe_model(http: HttpService, model_id: str) -> str:
    """Probe ONE model through the gateway (the Test pill, as a tool).

    No catalog-membership guard: the gateway accepts raw/aliased ids too, so
    a miss in /v1/models is not proof of a bad id — the gateway itself gives
    the verdict (and a genuinely bad id fails there, honestly).
    """
    endpoint = ""
    with contextlib.suppress(Exception):
        response = await http.get(f"{_root()}/v1/models")
        for row in response.json().get("data") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == model_id:
                endpoint = str(row.get("endpoint_type") or "")
                break
    result = await model_bench.test_model(http, state.gateway_base_url, model_id, endpoint)
    verdict = result.get("verdict")
    ms = result.get("ms")
    snippet = str(result.get("snippet") or "")
    if verdict == "OK":
        return f"{model_id}: OK in {ms}ms. Reply starts: {snippet[:80]}"
    if verdict == "RATE":
        return f"{model_id}: rate limited (probed twice, 4s apart). Try another model."
    if verdict == "EMPTY":
        return f"{model_id}: answered but returned no text ({ms}ms)."
    return f"{model_id}: failed ({ms}ms). {snippet[:120]}"


def build_status_tool(http: HttpService) -> AIFunction:
    """kani tool: live gateway health/counts."""

    async def gateway_health() -> str:
        """Get the local gateway's live health: uptime, model counts, and
        source responsiveness. Use when the user asks whether the router is
        up, how many models are ready, or what is currently rate limited."""
        return await gateway_status(http)

    return AIFunction(gateway_health, name="gateway_status", auto_retry=False)


def build_models_tool(http: HttpService) -> AIFunction:
    """kani tool: the catalog, optionally filtered by status."""

    async def list_gateway_models(
        status_filter: Annotated[
            str,
            AIParam(
                "Optional status to filter by: active, rate limited, slow, "
                "failed (or the raw words untested). Empty lists everything."
            ),
        ] = "",
    ) -> str:
        """List the gateway's models in its own server order, one per line,
        with live status. Use before recommending a model or when the user
        asks what is available / rate limited right now."""
        return await list_models(http, status_filter)

    return AIFunction(
        list_gateway_models,
        name="list_models",
        auto_retry=False,
        auto_truncate=6000,
    )


def build_probe_tool(http: HttpService) -> AIFunction:
    """kani tool: single-model probe (the Test pill)."""

    async def test_model(
        model_id: Annotated[
            str,
            AIParam("The exact model id to probe, as shown by list_models."),
        ],
    ) -> str:
        """Probe ONE model through the gateway and report the verdict
        (OK / rate limited / failed / empty). Cheap and local; use when the
        user asks to test or retry a specific model. For re-testing every
        not-ready model, the user should press 'Retest not-ready' in the
        app, which is deliberately not a tool."""
        return await probe_model(http, model_id)

    return AIFunction(test_model, name="test_model", auto_retry=False)
