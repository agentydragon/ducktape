"""Chromium executable and launch flags shared by browser tests."""

import json

from util.bazel.runfiles import find_path, get_required_path, own_repo_rlocation

_FLAGS = json.loads(get_required_path(own_repo_rlocation("util/testing/browser/chromium-flags.json")).read_text())
CONTAINER_BASE_BROWSER_ARGS: list[str] = _FLAGS["containerBase"]
DETERMINISTIC_BROWSER_ARGS: list[str] = CONTAINER_BASE_BROWSER_ARGS + _FLAGS["deterministicExtra"]


def chromium_executable() -> str | None:
    """Resolve the hermetic browser, or let Playwright select one outside Bazel."""
    return str(path) if (path := find_path("chrome_headless_shell/chrome-headless-shell")) else None
