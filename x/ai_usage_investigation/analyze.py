"""Summarize quota, daily-token, and raw accounting observations."""

import argparse
import bisect
import gzip
import json
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Poll:
    at: datetime
    used: float
    reset: datetime | None


@dataclass(frozen=True)
class ObservedRate:
    as_of: str
    points_per_hour: float
    lower: float
    upper: float
    start_bracket_seconds: float


@dataclass(frozen=True)
class UnavailableRate:
    reason: str


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def discontinuity(before: Poll, after: Poll) -> bool:
    if after.used < before.used:
        return True
    if (before.reset is None) != (after.reset is None):
        return True
    return (
        before.reset is not None and after.reset is not None and abs((after.reset - before.reset).total_seconds()) > 60
    )


def measure_hour(polls: Sequence[Poll], *, cutoff: datetime) -> ObservedRate | UnavailableRate:
    """Bracket the hour's start; envelope assumes stable one-point quantization.

    This is a gauge measurement conditional on no hidden reset or lag. It is
    neither a statistical confidence interval nor a token consumption bound.
    """
    end = polls[-1]
    if (cutoff - end.at).total_seconds() > 900:
        return UnavailableRate(reason="stale series")
    start = end.at - timedelta(hours=1)
    times = [p.at for p in polls]
    left = bisect.bisect_right(times, start) - 1
    if left < 0:
        return UnavailableRate(reason="less than one hour of history")
    right = left if polls[left].at == start else left + 1
    selected = polls[left:]
    if any((b.at - a.at).total_seconds() > 900 for a, b in pairwise(selected)):
        return UnavailableRate(reason="gap longer than 15 minutes")
    if any(discontinuity(a, b) for a, b in pairwise(selected)):
        return UnavailableRate(reason="reset or discontinuity inside measurement bracket")
    if any(p.used >= 100 for p in selected):
        return UnavailableRate(reason="saturated gauge; consumption is censored")
    low_start, high_start = polls[left].used, polls[right].used
    return ObservedRate(
        as_of=end.at.isoformat(),
        points_per_hour=end.used - (low_start + high_start) / 2,
        lower=max(0, end.used - high_start - 1),
        upper=max(0, end.used - low_start + 1),
        start_bracket_seconds=(polls[right].at - polls[left].at).total_seconds(),
    )


def read_rows(path: Path) -> list[dict[str, Any]]:
    body = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    return [json.loads(line) for line in body.splitlines() if line]


def summarize_quota(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, int, str], list[Poll]] = defaultdict(list)
    seen: set[tuple[str, str, int, str]] = set()
    for row in rows:
        key = (row["provider"], row["window_seconds"], row["window_name"])
        identity = (row["event_id"], *key)
        if identity in seen:
            continue
        seen.add(identity)
        groups[key].append(
            Poll(
                parse_time(row["observed_at"]),
                row["used_percent"],
                parse_time(row["reset_at"]) if row["reset_at"] else None,
            )
        )
    cutoff = max(p.at for group in groups.values() for p in group)
    summaries, timelines = [], []
    for (provider, seconds, name), polls in groups.items():
        polls.sort(key=lambda p: p.at)
        gaps = [(b.at - a.at).total_seconds() for a, b in pairwise(polls)]
        drops = []
        for before, after in pairwise(polls):
            if after.used < before.used:
                early = before.reset is not None and after.at < before.reset - timedelta(minutes=10)
                drops.append(
                    {
                        "at": after.at.isoformat(),
                        "before": before.used,
                        "after": after.used,
                        "early": early,
                        "gap_seconds": (after.at - before.at).total_seconds(),
                    }
                )
        summaries.append(
            {
                "provider": provider,
                "window_seconds": seconds,
                "name": name,
                "n": len(polls),
                "first": polls[0].at.isoformat(),
                "last": polls[-1].at.isoformat(),
                "fractional": sum(p.used != round(p.used) for p in polls),
                "median_gap_seconds": statistics.median(gaps),
                "p95_gap_seconds": sorted(gaps)[int(0.95 * len(gaps))],
                "max_gap_hours": max(gaps) / 3600,
                "gaps_over_15m": sum(g > 900 for g in gaps),
                "hour": asdict(measure_hour(polls, cutoff=cutoff)),
                "drops": drops,
            }
        )
        # Half-hour sampling plus both sides of every drop/gap. Mark breaks before
        # drawing: thinning must never connect a reset or outage into a slope.
        keep = {0, len(polls) - 1}
        for i in range(1, len(polls)):
            if (
                polls[i].at.timestamp() // 1800 != polls[i - 1].at.timestamp() // 1800
                or discontinuity(polls[i - 1], polls[i])
                or gaps[i - 1] > 900
            ):
                keep.update((i - 1, i))
        points, previous = [], 0
        for i in sorted(keep):
            broken = any(discontinuity(polls[j - 1], polls[j]) or gaps[j - 1] > 900 for j in range(previous + 1, i + 1))
            points.append([polls[i].at.timestamp() * 1000, polls[i].used, broken])
            previous = i
        timelines.append({"provider": provider, "window_seconds": seconds, "name": name, "points": points})
    return summaries, timelines


def summarize_daily(rows: list[dict[str, Any]]) -> dict[str, Any]:
    weeks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = date.fromisoformat(row["start_date"])
        weeks[(day - timedelta(days=day.weekday())).isoformat()].append(row)
    return {
        "days_reported": len(rows),
        "first": rows[0]["start_date"],
        "last": rows[-1]["start_date"],
        "days_latest_below_prior_max": sum(r["latest_tokens"] < r["max_reported"] for r in rows),
        "weeks": [
            {"start": week, "days_reported": len(days), "tokens": sum(r["latest_tokens"] for r in days)}
            for week, days in sorted(weeks.items())
        ],
        "days": rows,
    }


def summarize_accounting(rows: list[dict[str, Any]]) -> dict[str, Any]:
    months: dict[str, list[tuple[str, float]]] = defaultdict(list)
    credit_days: dict[str, dict[str, Any]] = {}
    spend_days: dict[str, dict[str, Any]] = {}
    surface: dict[str, Any] | None = None
    for row in rows:
        if row["source"] == "claude" and row["claude_spend_used"] not in ("", "null"):
            amount = json.loads(row["claude_spend_used"])
            if amount["currency"] != "USD":
                raise ValueError(f"unsupported currency: {amount['currency']}")
            usd = amount["amount_minor"] / 10 ** amount["exponent"]
            months[row["observed_at"][:7]].append((row["observed_at"], usd))
            spend_days[row["observed_at"][:10]] = {"at": row["observed_at"], "usd": usd}
        if row["source"] == "codex" and row["codex_paid"] not in ("", "null"):
            credit_days[row["observed_at"][:10]] = {"at": row["observed_at"], **json.loads(row["codex_paid"])}
        if row["surface_breakdown"]:
            surface = json.loads(row["surface_breakdown"])
    return {
        "claude_months": [
            {"month": m, "first": v[0], "last": v[-1], "max_usd": max(x[1] for x in v)}
            for m, v in sorted(months.items())
        ],
        "claude_daily_counter": list(spend_days.values()),
        "codex_daily_balance": list(credit_days.values()),
        "latest_claude_surface_breakdown": surface,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    quota, timeline = summarize_quota(read_rows(args.input_dir / "quota-polls.jsonl"))
    result: dict[str, Any] = {
        "quota": quota,
        "timeline": timeline,
        "codex_daily": summarize_daily(read_rows(args.input_dir / "codex-daily.jsonl")),
        "accounting": summarize_accounting(read_rows(args.input_dir / "raw-accounting.jsonl")),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "quota": [{k: v for k, v in q.items() if k != "drops"} for q in quota],
                "codex_weeks": result["codex_daily"]["weeks"][-6:],
                "claude_months": result["accounting"]["claude_months"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
