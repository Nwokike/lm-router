"""Service singletons injected at render time (ServiceCtx chain)."""

from dataclasses import dataclass
from typing import Any

import flet as ft


@dataclass
class Services:
    settings: Any = None
    http: Any = None
    engine: Any = None
    agent: Any = None
    search: Any = None
    providers: Any = None
    mcp: Any = None
    ads: Any = None
    update: Any = None


ServiceCtx = ft.create_context(Services())
