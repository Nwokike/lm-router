"""Guard: every `ft.<Name>` referenced in src/ must exist on the real flet package.

flet 1.0 resolves control names through a lazy _LAZY table; unknown names raise
AttributeError only in real app runs (the test-render harness masked a
ChoiceChip bug that crashed `flet run`). This static check resolves every
reference with hasattr() — which triggers the same lazy import — so a missing
control fails tests instead of the user's app.
"""

import re
from pathlib import Path

import flet as ft

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def test_all_ft_references_exist() -> None:
    bad: list[str] = []
    pattern = re.compile(r"\bft\.([A-Z]\w*)\b")
    for path in SRC.rglob("*.py"):
        if "assets" in path.parts and "engine" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = match.group(1)
            if not hasattr(ft, name):
                bad.append(f"{path.relative_to(ROOT)}: ft.{name} does not exist")
    assert not bad, "Missing flet controls referenced in src/:\n" + "\n".join(bad)


def test_all_enum_member_references_exist() -> None:
    """Generic `ft.Parent.MEMBER` check against the installed .venv flet.

    Covers every enum-ish reference (Icons, MarkdownCodeTheme, BoxFit,
    Colors, ScrollMode, SnackBarBehavior, ...) without a hand-maintained
    list — always verified against the real package per project rule.
    """
    bad: list[str] = []
    pattern = re.compile(r"\bft\.([A-Z]\w*)\.([A-Z]\w*)\b")
    for path in SRC.rglob("*.py"):
        if "assets" in path.parts and "engine" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            parent, member = match.group(1), match.group(2)
            parent_obj = getattr(ft, parent, None)
            if parent_obj is None:
                continue  # missing parent reported by test_all_ft_references_exist
            if not hasattr(parent_obj, member):
                bad.append(f"{path.relative_to(ROOT)}: ft.{parent}.{member} does not exist")
    assert not bad, "Missing flet enum members referenced in src/:\n" + "\n".join(bad)


def test_lowercase_enum_members_exist() -> None:
    """`ft.Parent.lowercase_helper` typos (Padding.symmetric, Colors.with_opacity)."""
    bad: list[str] = []
    pattern = re.compile(r"\bft\.([A-Z]\w*)\.([a-z]\w*)\b")
    for path in SRC.rglob("*.py"):
        if "assets" in path.parts and "engine" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            parent, member = match.group(1), match.group(2)
            parent_obj = getattr(ft, parent, None)
            if parent_obj is None:
                continue
            if not hasattr(parent_obj, member):
                bad.append(f"{path.relative_to(ROOT)}: ft.{parent}.{member} does not exist")
    assert not bad, "Missing flet helper members referenced in src/:\n" + "\n".join(bad)


def test_dynamic_and_from_imports_exist() -> None:
    """`from flet import X` and `getattr(ft.X, "name")` bypass the regexes."""
    bad: list[str] = []
    from_pattern = re.compile(r"from flet import ([A-Za-z_][\w, ]*)")
    getattr_pattern = re.compile(r"getattr\(ft\.(\w+),\s*[\"']([\w]+)[\"']")
    for path in SRC.rglob("*.py"):
        if "assets" in path.parts and "engine" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in from_pattern.finditer(text):
            for raw in match.group(1).split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and not hasattr(ft, name):
                    bad.append(f"{path.relative_to(ROOT)}: from flet import {name} missing")
        for match in getattr_pattern.finditer(text):
            parent, member = match.group(1), match.group(2)
            parent_obj = getattr(ft, parent, None)
            if parent_obj is None:
                continue
            try:
                exists = hasattr(parent_obj, member)
            except Exception:
                # Runtime property that needs a live page (ft.context.page):
                # it exists but cannot be probed outside the app.
                continue
            if not exists:
                bad.append(f"{path.relative_to(ROOT)}: getattr(ft.{parent}, {member!r}) missing")
    assert not bad, "Missing dynamic flet references in src/:\n" + "\n".join(bad)


def test_constructor_kwargs_exist() -> None:
    """Every ft.X(...) keyword in src/ must be a real constructor parameter.

    hasattr-style checks cannot see kwargs: TextField(font_family=...) shipped
    and bricked the Server tab the moment a user enabled "Require API key",
    because every render test used default settings and the name/enum tests
    only check that ft.TextField EXISTS. This closes that exact hole.
    """
    import ast
    import inspect

    bad: list[str] = []
    signatures: dict[str, set[str] | None] = {}

    def _params(name: str) -> set[str] | None:
        if name not in signatures:
            cls = getattr(ft, name, None)
            if cls is None or not callable(cls):
                signatures[name] = None
            else:
                try:
                    parameters = inspect.signature(cls).parameters
                except TypeError, ValueError:
                    signatures[name] = None
                else:
                    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                        signatures[name] = None  # accepts anything
                    else:
                        signatures[name] = set(parameters)
        return signatures[name]

    for path in SRC.rglob("*.py"):
        if "assets" in path.parts and "engine" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "ft"
            ):
                continue
            accepted = _params(func.attr)
            if accepted is None:
                continue  # unknown name / un-signatured / **kwargs: covered above
            for kw in node.keywords:
                if kw.arg is None:
                    continue  # **kwargs splat at the call site
                if kw.arg not in accepted:
                    bad.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} ft.{func.attr}({kw.arg}=...)"
                    )
    assert not bad, "Unknown constructor kwargs in src/:\n" + "\n".join(bad)
