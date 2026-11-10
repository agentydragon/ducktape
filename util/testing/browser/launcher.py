"""Chromium executable and launch flags shared by browser tests."""

import json

from python.runfiles import Runfiles

_RUNFILES = Runfiles.CreateOrRaise()
_FLAGS = json.loads((_RUNFILES.root() / "ducktape/util/testing/browser/chromium-flags.json").read_text())
CONTAINER_BASE_BROWSER_ARGS: list[str] = _FLAGS["containerBase"]
DETERMINISTIC_BROWSER_ARGS: list[str] = CONTAINER_BASE_BROWSER_ARGS + _FLAGS["deterministicExtra"]


def chromium_executable() -> str | None:
    """Resolve the hermetic browser, or let Playwright select one if absent."""
    path = _RUNFILES.root() / "chrome_headless_shell/chrome-headless-shell"
    return str(path) if path.exists() else None
