"""Desktop-local tools: shell runs commands, read returns files, neither raises."""

from __future__ import annotations

from test_boot_smoke import boot_page

__all__ = ["boot_page"]


def test_shell_runs_and_reports_exit() -> None:
    import asyncio

    from services.local_tools import build_shell_tool

    tool = build_shell_tool()
    assert tool.name == "shell"
    out = asyncio.run(tool(command="echo hello-local"))
    assert "hello-local" in out
    assert "exit=0" in out


def test_shell_timeout_is_an_error_string(monkeypatch) -> None:
    import asyncio
    import subprocess

    import services.local_tools as lt_mod

    async def _slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=30)

    monkeypatch.setattr(lt_mod.asyncio, "to_thread", _slow)
    out = asyncio.run(lt_mod.build_shell_tool()(command="sleep 999"))
    assert "timed out" in out


def test_shell_empty_command() -> None:
    import asyncio

    from services.local_tools import build_shell_tool

    out = asyncio.run(build_shell_tool()(command="   "))
    assert "No command" in out


def test_read_returns_content(tmp_path) -> None:
    import asyncio

    from services.local_tools import build_read_tool

    target = tmp_path / "note.txt"
    target.write_text("line one\nline two\n", encoding="utf-8")
    out = asyncio.run(build_read_tool()(path=str(target)))
    assert "line one" in out and "line two" in out
    assert "lines" in out


def test_read_missing_is_an_error_string(tmp_path) -> None:
    import asyncio

    from services.local_tools import build_read_tool

    out = asyncio.run(build_read_tool()(path=str(tmp_path / "nope.txt")))
    assert "not found" in out.lower()


def test_read_refuses_directories(tmp_path) -> None:
    import asyncio

    from services.local_tools import build_read_tool

    out = asyncio.run(build_read_tool()(path=str(tmp_path)))
    assert "directory" in out.lower()


def test_desktop_registers_shell_and_read(boot_page) -> None:
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    tools, generation = controller._extra_tools()
    names = {getattr(tool, "name", "") for tool in tools}
    assert {"shell", "read"} <= names, names
    assert "router4" in str(generation)
