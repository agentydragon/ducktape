"""One OTel tracer for system-test setup, scenarios, and cleanup."""

import json
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer(__name__)


async def traced_scenario(name: str, scenario: Coroutine[Any, Any, None]) -> None:
    with tracer.start_as_current_span("system_test.scenario", attributes={"scenario": name}):
        await scenario


def timing_summary(path: Path) -> str:
    if not path.exists():
        return "No completed system-test spans."
    durations: dict[str, list[float]] = {}
    for line in path.read_text().splitlines():
        span = json.loads(line)
        if not span["name"].startswith("system_test."):
            continue
        attributes = span["attributes"]
        label = span["name"]
        for key in ("image", "deployment.name", "scenario", "harness", "pytest.nodeid"):
            if key in attributes:
                label += f" [{attributes[key]}]"
        seconds = (
            datetime.fromisoformat(span["end_time"]) - datetime.fromisoformat(span["start_time"])
        ).total_seconds()
        durations.setdefault(label, []).append(seconds)
    return "\n".join(
        f"{sum(values):8.2f}s total / {max(values):.2f}s max / {len(values)} spans  {label}"
        for label, values in sorted(durations.items(), key=lambda item: -sum(item[1]))[:40]
    )
