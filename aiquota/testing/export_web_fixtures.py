"""Emit the web dashboard's test fixtures from the shared YAML scenarios.

Two files, both generated so neither can drift from the Python it mirrors:

- **scenes** — exactly what `/v1/quotas` serves for each scenario in `FIXTURE_NAMES`, so the
  browser renders the API's payload rather than a hand-written imitation of it, and the web
  screenshots cover the same states as the GNOME renders and the CLI snapshot.
- **format cases** — every window in those scenes with the strings `aiquota/render/format.py`
  and `aiquota/pace.py` produce for it. `aiquota/frontend/format.test.ts` holds the TypeScript
  port to these, so the dashboard cannot quietly disagree with the CLI about a pace, a
  forecast, or a tint.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aiquota.models import FetchSuccess, QuotaWindow
from aiquota.pace import compute_pace, is_exhausted, tint_for
from aiquota.render.format import (
    display_used_percent,
    format_duration,
    format_pace,
    format_pace_forecast,
    format_window_label,
)
from aiquota.render.view_model import AllQuotasView, ProviderView, to_view
from aiquota.testing.quota_fixtures import DEFAULT_NOW, FIXTURE_NAMES, load_quota_fixture
from util.bazel.runfiles import get_required_path


def scene_payloads(*, now: datetime = DEFAULT_NOW) -> dict[str, Any]:
    return {
        name: to_view(load_quota_fixture(_fixture_path(name), now=now), now).model_dump(mode="json")
        for name in FIXTURE_NAMES
    }


def _fixture_path(name: str) -> Path:
    return get_required_path(f"_main/aiquota/testing/fixtures/{name}.yaml")


def format_cases(views: list[AllQuotasView]) -> list[dict[str, Any]]:
    """One case per distinct window across every scene, with its rendered strings."""
    cases: dict[tuple[str | None, float, float, float, bool], dict[str, Any]] = {}
    for view in views:
        for provider in view.providers:
            for window, is_short in _windows(provider):
                key = (window.name, window.used_percent, window.reset_seconds, window.window_seconds, is_short)
                cases[key] = _case(window, is_short=is_short)
    return sorted(cases.values(), key=lambda case: (case["window_seconds"], case["used_percent"], case["is_short"]))


def _windows(provider: ProviderView) -> list[tuple[QuotaWindow, bool]]:
    results = [provider.last_output.result] + ([provider.last_success.result] if provider.last_success else [])
    pairs: list[tuple[QuotaWindow, bool]] = []
    for result in results:
        if not isinstance(result, FetchSuccess) or not result.windows:
            continue
        longest = max(window.window_seconds for window in result.windows)
        pairs.extend((window, window.window_seconds < longest) for window in result.windows)
    return pairs


def _case(window: QuotaWindow, *, is_short: bool) -> dict[str, Any]:
    pace = None if is_exhausted(window) else compute_pace(window)
    return {
        "used_percent": window.used_percent,
        "reset_seconds": window.reset_seconds,
        "window_seconds": window.window_seconds,
        "is_short": is_short,
        "name": window.name,
        "expected": {
            "label": format_window_label(window),
            "used_percent": display_used_percent(window),
            "reset": format_duration(window.reset_seconds),
            "pace": format_pace(pace),
            "forecast": format_pace_forecast(pace, window.reset_seconds),
            "tint": tint_for(pace, window.used_percent, is_short=is_short),
        },
    }


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=Path, required=True, help="Where to write the /v1/quotas payload per scene")
    parser.add_argument("--format-cases", type=Path, required=True, help="Where to write the rendered-string goldens")
    args = parser.parse_args()

    scenes = scene_payloads()
    _write(args.scenes, scenes)
    _write(args.format_cases, format_cases([AllQuotasView.model_validate(scene) for scene in scenes.values()]))


if __name__ == "__main__":
    main()
