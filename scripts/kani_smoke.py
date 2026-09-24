"""CLI smoke test: exercise every kani API our app uses against a LIVE gateway.

This is the CLI twin of the GUI: same gateway, same kani surface, no Flet.
Run it whenever the router or the chat stack changes:

    # terminal 1 — fresh gateway (or just open the app once):
    curl -fsSL https://router.kiri.ng/run.py | py -3 -
    # terminal 2:
    uv run python scripts/kani_smoke.py --base http://127.0.0.1:8082/v1

Coverage (mirrors src/services/agent.py, src/services/history.py,
src/services/search.py, src/services/mcp.py usage):
  engine ctor (client + chat_completions + max_context_size), Kani ctor with
  system_prompt/chat_history/functions/retry_attempts, full_round_stream with
  StreamManager role/message/chunk iteration, openai_usage extras,
  chat_history append/clear, get_prompt + engine.prompt_len metering,
  kani save/load roundtrip, AIFunction with AIParam-annotated params +
  auto_truncate, FunctionCall -> do_function_call (the on_tool path),
  hyperparams passthrough, mid-stream cancel + recovery, and the exact
  openai exception classes our error mapper catches.

Exit code 0 = every check PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import openai
from kani import AIFunction, ChatMessage, FunctionCall, Kani
from kani.ai_function import AIParam
from kani.engines.openai import OpenAIEngine

from core.state import is_chat_eligible

RESULTS: list[tuple[str, str, str]] = []


def record(api: str, ok: bool, detail: object = "") -> None:
    RESULTS.append((api, "PASS" if ok else "FAIL", str(detail)[:220]))
    print(f"[{'PASS' if ok else 'FAIL'}] {api}" + (f" — {detail}" if detail else ""))


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def fn_schema(fn: AIFunction) -> dict:
    return dict(getattr(fn, "json_schema", {}) or {})


def make_client(base: str) -> openai.AsyncOpenAI:
    # Exactly the shape src/services/agent.py:_default_engine_factory builds.
    return openai.AsyncOpenAI(
        api_key="lm-router",
        base_url=base,
        timeout=httpx.Timeout(90.0, connect=5.0),
        max_retries=2,
    )


async def pick_model(base: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(f"{base}/models")
        resp.raise_for_status()
        models = resp.json().get("data", [])
    eligible = [m for m in models if m.get("id") and is_chat_eligible(m)]
    if not eligible:
        raise SystemExit("gateway returned no chat-eligible models")
    for m in eligible:
        if m["id"] == "auto":
            return "auto"
    return eligible[0]["id"]


async def full_round_capture(k: Kani, prompt: str, **kw) -> tuple[str, object]:
    """Consume full_round_stream exactly like src/services/agent.py does."""
    text = ""
    final_msg = None
    tools_seen: list[tuple[str, str, bool]] = []
    async for manager in k.full_round_stream(prompt, **kw):
        role = str(getattr(manager, "role", ""))
        if "function" in role.lower():
            msg = await manager.message()
            tools_seen.append(
                (
                    str(getattr(msg, "name", "tool")),
                    str(getattr(msg, "text", "") or ""),
                    bool(getattr(msg, "is_tool_call_error", False)),
                )
            )
            continue
        async for chunk in manager:
            text += chunk or ""
        final_msg = await manager.message()
    return text, final_msg


async def run(base: str, model: str) -> None:
    client = make_client(base)

    # 1. Engine construction (app factory shape)
    try:
        engine = OpenAIEngine(
            client=client,
            model=model,
            api_type="chat_completions",
            max_context_size=128_000,
        )
        record("OpenAIEngine(client, chat_completions, max_context_size)", True, model)
    except Exception as exc:
        record("OpenAIEngine(...)", False, f"{type(exc).__name__}: {exc}")
        return

    # 2. Kani ctor — every kwarg our app passes
    async def echo_tool(
        text: Annotated[str, AIParam("Text to echo back")],
    ) -> str:
        """Echo the text back. Used by the smoke test."""
        return f"echo: {text}"

    ai_fn = AIFunction(echo_tool, name="echo_tool", auto_truncate=8000)
    try:
        k = Kani(
            engine,
            system_prompt="You are a smoke-test bot. Answers must be short.",
            chat_history=[],
            functions=[ai_fn],
            retry_attempts=1,
        )
        ctor_ok = bool(k.system_prompt) and "echo_tool" in k.functions
        record(
            "Kani(system_prompt, chat_history, functions, retry_attempts)",
            ctor_ok,
            f"functions={list(k.functions)}",
        )
        if not ctor_ok:
            return
    except Exception as exc:
        record("Kani ctor", False, f"{type(exc).__name__}: {exc}")
        return

    # 3. full_round_stream — streaming, StreamManager, usage extras
    # NOTE: generous max_tokens — reasoning models (auto rotates) burn small
    # caps on reasoning and leave content empty (observed live: ~34 reasoning
    # tokens before the first content delta).
    try:
        text, msg = await asyncio.wait_for(
            full_round_capture(k, "Reply with exactly: SMOKE-OK", max_tokens=512),
            timeout=120,
        )
        usage = getattr(msg, "extra", {}) or {}
        usage = usage.get("openai_usage") if hasattr(usage, "get") else None
        ok = bool(text.strip()) and msg is not None and usage is not None
        record(
            "full_round_stream + StreamManager + openai_usage",
            ok,
            f"text={text.strip()[:40]!r} usage_keys={sorted(usage) if usage else None}",
        )
    except Exception as exc:
        record("full_round_stream", False, f"{type(exc).__name__}: {exc}")

    # 4. chat_history mutation + metering (get_prompt / engine.prompt_len)
    try:
        k.chat_history.append(ChatMessage.user("remember this phrase"))
        prompt_msgs = await maybe_await(k.get_prompt())
        plen = await maybe_await(engine.prompt_len(prompt_msgs, None))
        k.chat_history.clear()
        ok = plen > 0
        record("chat_history append/clear + get_prompt + engine.prompt_len", ok, f"plen={plen}")
    except Exception as exc:
        record("chat_history/get_prompt/prompt_len", False, f"{type(exc).__name__}: {exc}")

    # 5. save / load roundtrip (src/services/history.py path)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "smoke.json")
            k.chat_history.append(ChatMessage.user("persisted user line"))
            k.save(path)
            k2 = Kani(engine, system_prompt="reload")
            k2.load(path)
            ok = len(k2.chat_history) >= 1
            record("kani save/load (history persistence)", ok, f"loaded={len(k2.chat_history)}")
            k.chat_history.clear()
    except Exception as exc:
        record("kani save/load", False, f"{type(exc).__name__}: {exc}")

    # 6. AIFunction call (async inner + AIParam schema + auto_truncate)
    # AIParamSchema has a default object repr — assert on real attributes:
    # .name / .description must reflect the Annotated[..., AIParam(...)].
    try:
        params = list(ai_fn.get_params())
        out = await maybe_await(ai_fn("hello"))
        text_param = next((p for p in params if getattr(p, "name", None) == "text"), None)
        has_desc = bool(getattr(text_param, "description", ""))
        schema_ok = "Text to echo back" in str(fn_schema(ai_fn))
        ok = (
            "echo: hello" in str(out)
            and text_param is not None
            and has_desc
            and schema_ok
            and ai_fn.auto_truncate == 8000
        )
        record(
            "AIFunction call + AIParam params + auto_truncate",
            ok,
            f"name={getattr(text_param, 'name', None)!r} "
            f"desc={getattr(text_param, 'description', None)!r}",
        )
    except Exception as exc:
        record("AIFunction", False, f"{type(exc).__name__}: {exc}")

    # 7. do_function_call — the on_tool path (FunctionCall -> message attrs)
    try:
        call = FunctionCall(name="echo_tool", arguments=json.dumps({"text": "via-call"}))
        result = await asyncio.wait_for(k.do_function_call(call, "smoke-id-1"), timeout=60)
        message = getattr(result, "message", result)
        detail = (
            f"name={getattr(message, 'name', None)!r} "
            f"text={str(getattr(message, 'text', ''))[:40]!r} "
            f"is_error={getattr(message, 'is_tool_call_error', False)}"
        )
        record(
            "do_function_call (tool result attrs)",
            getattr(message, "name", None) is not None,
            detail,
        )
    except Exception as exc:
        record("do_function_call", False, f"{type(exc).__name__}: {exc}")

    # 8. Hyperparams passthrough (temperature/max_tokens on the stream)
    try:
        text, _ = await asyncio.wait_for(
            full_round_capture(
                k,
                "Reply with exactly one word: HP",
                temperature=0.0,
                max_tokens=512,
            ),
            timeout=120,
        )
        record("full_round_stream hyperparams (temperature, max_tokens)", "HP" in text, text[:30])
    except Exception as exc:
        record("hyperparams passthrough", False, f"{type(exc).__name__}: {exc}")

    # 9. Mid-stream cancel + recovery (our Stop path; busy-stuck regression)
    try:
        task = asyncio.create_task(
            full_round_capture(
                k,
                "Write 40 sentences about testing. One per line.",
                max_tokens=400,
            ),
        )
        await asyncio.sleep(1.5)
        if not task.done():
            task.cancel()
            with_suppressed = False
            try:
                await task
            except asyncio.CancelledError:
                with_suppressed = True
            record("mid-stream cancel", with_suppressed, "CancelledError propagated cleanly")
        else:
            record("mid-stream cancel", True, "model finished before cancel window")
        text2, _ = await asyncio.wait_for(
            full_round_capture(k, "Reply with exactly: AFTER-CANCEL", max_tokens=512),
            timeout=120,
        )
        record("turn after cancel (no busy-lock)", "AFTER-CANCEL" in text2, text2.strip()[:30])
    except Exception as exc:
        record("cancel/recovery", False, f"{type(exc).__name__}: {exc}")

    # 10. Bad model -> exact exception classes our error mapper catches
    try:
        bad_engine = OpenAIEngine(
            client=make_client(base),
            model="definitely-not-a-real-model-xyz",
            api_type="chat_completions",
            max_context_size=8192,
        )
        bad_k = Kani(bad_engine, system_prompt="x")
        expected = (
            openai.AuthenticationError,
            openai.NotFoundError,
            openai.BadRequestError,
            openai.PermissionDeniedError,
            openai.APIStatusError,
            openai.APIConnectionError,
        )
        try:
            await asyncio.wait_for(
                full_round_capture(bad_k, "hi", max_tokens=16),
                timeout=60,
            )
            record("bad model -> typed openai error", False, "no exception raised")
        except expected as exc:
            record("bad model -> typed openai error", True, type(exc).__name__)
        except Exception as exc:
            record(
                "bad model -> typed openai error",
                False,
                f"UNEXPECTED {type(exc).__name__}: {exc}",
            )
    except Exception as exc:
        record("bad model scenario setup", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8082/v1", help="gateway base URL")
    parser.add_argument("--model", default="", help="model id (default: auto-pick chat-eligible)")
    args = parser.parse_args()

    async def _go() -> None:
        async with httpx.AsyncClient(timeout=5.0) as http:
            health = await http.get(f"{args.base.rsplit('/v1', 1)[0]}/health")
            health.raise_for_status()
        model = args.model or (await pick_model(args.base))
        print(f"gateway OK · testing with model: {model}")
        await run(args.base, model)

    try:
        asyncio.run(_go())
    except httpx.HTTPError as exc:
        print(f"FATAL: gateway unreachable at {args.base}: {exc}")
        return 2

    print("\n==== SUMMARY ====")
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    for api, verdict, detail in RESULTS:
        print(f"{verdict:4}  {api}" + (f"  ({detail})" if detail and verdict == "FAIL" else ""))
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
