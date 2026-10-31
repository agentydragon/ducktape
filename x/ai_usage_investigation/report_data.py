"""Assemble the usage/quota investigation from aggregate evidence only."""

import argparse
import csv
import gzip
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from x.ai_usage_investigation.analyze import (
    Poll,
    discontinuity,
    parse_time,
    read_rows,
    summarize_accounting,
    summarize_daily,
    summarize_quota,
)

TOKEN_FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def load_rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_text())
    return value if isinstance(value, list) else value["rows"]


def assemble_usage(sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for family, rows in sources.items():
        for original in rows:
            row = dict(original)
            if row["total_tokens"] is None and row["source"] == "claude_message":
                parts = [row[field] for field in TOKEN_FIELDS[:4]]
                if all(value is not None for value in parts):
                    row["total_tokens"] = sum(parts)
                    row["total_basis"] = "derived_from_nonoverlapping_message_categories"
            if family == "claude_code_web":
                row["total_basis"] = "derived_from_nonoverlapping_message_categories"
            model = row.get("canonical_model") or row["model"] or "unattributed"
            grouped[(row["bucket_start_utc"][:10], row["provider"], model, family, row["payment_lane"])].append(row)
    weekly = []
    for (week, provider, model, family, lane), rows in sorted(grouped.items()):
        result: dict[str, Any] = {
            "week": week,
            "provider": provider,
            "model": model,
            "source_group": family,
            "payment_lane": lane,
            "requests": sum(row["requests"] for row in rows),
            "missing_total_rows": sum(row["total_tokens"] is None for row in rows),
            "total_basis": "; ".join(sorted({row.get("total_basis", "source_reported") for row in rows})),
        }
        subtotals = {}
        for field in TOKEN_FIELDS:
            known = [row[field] for row in rows if row[field] is not None]
            subtotals[field] = sum(known)
            result[field] = sum(known) if len(known) == len(rows) else None
        result["known_token_subtotals"] = subtotals
        weekly.append(result)
    by_model: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in weekly:
        by_model[(row["week"], row["provider"], row["model"])].append(row)
    combined = []
    for (week, provider, model), rows in sorted(by_model.items()):
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_source[row["source_group"]].append(row)
        totals = {
            source: sum(row["known_token_subtotals"]["total_tokens"] for row in values)
            for source, values in by_source.items()
        }
        incomplete = any(row["missing_total_rows"] for row in rows)
        combined.append(
            {
                "week": week,
                "provider": provider,
                "model": model,
                "captured_total_lower": max(totals.values()),
                "captured_total_upper": None if incomplete else sum(totals.values()),
                "sources": sorted(totals),
                "source_totals": totals,
                "missing_total_rows": sum(row["missing_total_rows"] for row in rows),
            }
        )
    return {
        "rows": weekly,
        "combined_rows": combined,
        "accounting_note": "Source totals are observed records, not plan entitlements. Cross-source ranges bound the union of captured records under unresolved overlap; missing traffic can exceed them. Categories preserve cache/output overlap semantics; reasoning is an output subset.",
    }


def quota_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int, str], dict[str, Poll]] = defaultdict(dict)
    for row in rows:
        groups[(row["provider"], row["window_seconds"], row["window_name"])][row["event_id"]] = Poll(
            parse_time(row["observed_at"]),
            row["used_percent"],
            parse_time(row["reset_at"]) if row["reset_at"] else None,
        )
    result: list[dict[str, Any]] = []
    for (provider, seconds, name), polls in sorted(groups.items()):
        grouped: dict[str, dict[str, Any]] = {}
        for before, after in pairwise(sorted(polls.values(), key=lambda poll: poll.at)):
            week = (after.at.date() - timedelta(days=after.at.weekday())).isoformat()
            row = grouped.setdefault(
                week,
                {
                    "week": week,
                    "provider": provider,
                    "window_seconds": seconds,
                    "name": name,
                    "observed_increase_pp": 0,
                    "restored_headroom_pp": 0,
                    "observed_drops": 0,
                    "broken_pairs": 0,
                    "cross_week_pairs": 0,
                },
            )
            delta = after.used - before.used
            if delta < 0:
                row["observed_drops"] += 1
                row["restored_headroom_pp"] -= delta
            prior_week = (before.at.date() - timedelta(days=before.at.weekday())).isoformat()
            if prior_week != week:
                row["cross_week_pairs"] += 1
            if discontinuity(before, after) or after.at - before.at > timedelta(minutes=15):
                row["broken_pairs"] += 1
            elif prior_week == week:
                row["observed_increase_pp"] += delta
        result.extend(grouped.values())
    return result


def summarize_scoped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["limit_kind"], row["model_scope"])].append(row)
    result = []
    for (kind, name), values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda row: row["observed_at"])
        numeric = [row["used_percent"] for row in ordered if isinstance(row["used_percent"], (int, float))]
        result.append(
            {
                "kind": kind,
                "name": name,
                "observations": len(ordered),
                "first": ordered[0]["observed_at"],
                "last": ordered[-1]["observed_at"],
                "min_used_percent": min(numeric) if numeric else None,
                "max_used_percent": max(numeric) if numeric else None,
                "latest": ordered[-1],
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--native-weekly", type=Path, required=True)
    parser.add_argument("--cloud-weekly", type=Path, required=True)
    parser.add_argument("--langfuse-weekly", type=Path, required=True)
    parser.add_argument("--capacity", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    def extracted(name: str) -> list[dict[str, Any]]:
        plain = args.input_dir / name
        return read_rows(plain if plain.exists() else plain.with_suffix(plain.suffix + ".gz"))

    quota_rows = extracted("quota-polls.jsonl")
    quota, timeline = summarize_quota(quota_rows)
    usage = assemble_usage(
        {
            "native": load_rows(args.native_weekly),
            "claude_code_web": load_rows(args.cloud_weekly),
            "langfuse": load_rows(args.langfuse_weekly),
        }
    )
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "quota": quota,
        "timeline": timeline,
        "quota_weekly": quota_weekly(quota_rows),
        "codex_daily": summarize_daily(extracted("codex-daily.jsonl")),
        "accounting": summarize_accounting(extracted("raw-accounting.jsonl")),
        "usage": usage,
        "capacity": json.loads(args.capacity.read_text()),
        "scoped_quota": summarize_scoped(extracted("claude-scoped-quota.jsonl")),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    (args.output_dir / "weekly-model-usage.json").write_text(json.dumps(usage, indent=2) + "\n")
    with (args.output_dir / "weekly-model-usage.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["week", "provider", "model", "source_group", "payment_lane", "requests", *TOKEN_FIELDS],
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(usage["rows"])
    print(
        json.dumps(
            {
                "model_week_source_rows": len(usage["rows"]),
                "combined_model_weeks": len(usage["combined_rows"]),
                "capacity_series": len(result["capacity"]["results"]),
            }
        )
    )


if __name__ == "__main__":
    main()
