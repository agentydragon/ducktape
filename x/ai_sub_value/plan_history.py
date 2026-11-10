"""User-maintained exact subscription plan periods for quota calibration."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path


@dataclass(frozen=True)
class CodexPlanPeriod:
    start: datetime
    end: datetime | None
    plan: str


def _parse_utc(value: object, *, field: str, index: int) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Codex plan period {index} requires a {field} timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Codex plan period {index} has an invalid {field} timestamp") from error
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def read_codex_plan_history(path: Path) -> list[CodexPlanPeriod]:
    """Read exact plan periods from the local account-history JSON file."""
    value = json.loads(path.read_text())
    entries = value.get("periods") if isinstance(value, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("Codex plan history must contain a non-empty 'periods' list")
    periods = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("plan"), str) or not entry["plan"].strip():
            raise ValueError(f"Codex plan period {index} must have a non-empty plan label")
        start = _parse_utc(entry.get("start"), field="start", index=index)
        end_value = entry.get("end")
        end = _parse_utc(end_value, field="end", index=index) if end_value is not None else None
        if end is not None and end <= start:
            raise ValueError(f"Codex plan period {index} must end after it starts")
        periods.append(CodexPlanPeriod(start, end, entry["plan"].strip()))
    periods.sort(key=lambda period: period.start)
    for previous, current in pairwise(periods):
        if previous.end is None or previous.end > current.start:
            raise ValueError("Codex plan history periods must not overlap")
    return periods


def codex_plan_at(at: datetime, periods: list[CodexPlanPeriod]) -> CodexPlanPeriod | None:
    """Return the unique plan period containing `at`; intervals are half-open."""
    matches = [period for period in periods if period.start <= at and (period.end is None or at < period.end)]
    if len(matches) > 1:
        raise ValueError(f"overlapping Codex plan history at {at.isoformat()}")
    return matches[0] if matches else None


def validate_reported_codex_plan_types(period: CodexPlanPeriod, reported: set[str]) -> None:
    """Reject a manual period that spans a provider-reported plan-category change."""
    if len(reported) > 1:
        categories = ", ".join(sorted(reported))
        raise ValueError(
            f"Codex plan_type changed within annotated period {period.plan!r} "
            f"({categories}); split the plan history at the category change"
        )
