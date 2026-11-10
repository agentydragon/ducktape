"""Bundle privacy-safe Claude Code Web usage aggregates emitted by SQL."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

GRANULARITIES = ("5m", "hour", "week")
OUTPUT_NAMES = {"5m": "cloud-usage-5m.json.gz", "hour": "cloud-usage-hourly.json.gz", "week": "cloud-usage-weekly.json"}
NORMALIZED_FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
REQUIRED_USAGE_FIELDS = {
    "record_type",
    "granularity",
    "bucket_start_utc",
    "provider",
    "model",
    "source",
    "payment_lane",
    "requests",
    *NORMALIZED_FIELDS,
}


def json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: expected a JSON object") from error
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


def write_json(path: Path, value: dict[str, Any]) -> None:
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if path.suffix == ".gz":
        path.write_bytes(gzip.compress(body, mtime=0))
    else:
        path.write_bytes(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-jsonl", required=True, type=Path)
    parser.add_argument("--quality-jsonl", required=True, type=Path)
    parser.add_argument("--global-weekly-jsonl", required=True, type=Path)
    parser.add_argument("--cross-session-jsonl", required=True, type=Path)
    parser.add_argument("--payload-time-jsonl", required=True, type=Path)
    parser.add_argument("--provider-model-jsonl", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    usage = json_lines(args.usage_jsonl)
    metadata_rows = [row for row in usage if row.get("record_type") == "metadata"]
    usage_rows = [row for row in usage if row.get("record_type") == "usage"]
    if len(metadata_rows) != 1:
        raise ValueError(f"expected exactly one metadata row, got {len(metadata_rows)}")
    metadata = metadata_rows[0]
    for row in usage_rows:
        missing = REQUIRED_USAGE_FIELDS - row.keys()
        if missing:
            raise ValueError(f"usage row missing required keys: {sorted(missing)}")
        if row["granularity"] not in GRANULARITIES:
            raise ValueError(f"unexpected granularity: {row['granularity']!r}")

    quality_rows = json_lines(args.quality_jsonl)
    if len(quality_rows) != 1:
        raise ValueError(f"expected one quality audit row, got {len(quality_rows)}")
    cross_session_rows = json_lines(args.cross_session_jsonl)
    payload_time_rows = json_lines(args.payload_time_jsonl)
    if len(cross_session_rows) != 1 or len(payload_time_rows) != 1:
        raise ValueError("expected one cross-session audit row and one payload time audit row")
    provider_rows = json_lines(args.provider_model_jsonl)
    global_weekly_rows = json_lines(args.global_weekly_jsonl)
    global_metadata_rows = [row for row in global_weekly_rows if row.get("record_type") == "metadata"]
    global_usage_rows = [row for row in global_weekly_rows if row.get("record_type") == "usage"]
    if len(global_metadata_rows) != 1:
        raise ValueError(f"expected one global weekly metadata row, got {len(global_metadata_rows)}")
    for row in global_usage_rows:
        missing = REQUIRED_USAGE_FIELDS - row.keys()
        if missing:
            raise ValueError(f"global weekly row missing required keys: {sorted(missing)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for granularity in GRANULARITIES:
        rows = [dict(row) for row in usage_rows if row["granularity"] == granularity]
        rows.sort(
            key=lambda row: (
                row["bucket_start_utc"],
                row["provider"] or "",
                row["model"] or "",
                row["reported_service_tier"] or "",
                row["reported_speed"] or "",
            )
        )
        for row in rows:
            row.pop("record_type", None)
            row.pop("granularity", None)
            row.pop("total_tokens_definition", None)
        document = {
            "schema_version": 1,
            "source": "claude_code_web",
            "product_scope": "Claude Code Web archived sessions only; this is not API-key traffic. Stored usage does not distinguish included subscription use from overage or extra usage.",
            "granularity": granularity,
            "database": metadata.get("database"),
            "extracted_at_utc": metadata.get("extracted_at_utc"),
            "source_window": metadata.get("source_window"),
            "session_freshness": metadata.get("sessions"),
            "payment_lane": "unknown",
            "payment_lane_reason": metadata.get("payment_lane_reason"),
            "token_definitions": {
                "uncached_input_tokens": "message.usage.input_tokens",
                "cache_read_input_tokens": "message.usage.cache_read_input_tokens",
                "cache_write_input_tokens": "message.usage.cache_creation_input_tokens",
                "output_tokens": "message.usage.output_tokens, last stored event by sequence_num per session/message; per-message min/max retained as an observation envelope",
                "reasoning_output_tokens": "message.usage.output_tokens_details.thinking_tokens when supplied; subset of output_tokens, never add to totals",
                "total_tokens": "derived sum of uncached input + cache read + cache write + output; not a provider-reported total and not a billable amount",
            },
            "deduplication": metadata.get("dedup_scope"),
            "timestamp_definition": "UTC bucket uses the last stored assistant event by sequence_num per request, based on Web API events.created_at copied directly into the archive.",
            "coverage": "All retained assistant message events across all session environment kinds and main, subagent, and guardian messages; message IDs deduplicated per session, missing IDs retained per event.",
            "explicit_provider_models": metadata.get("explicit_provider_models"),
            "rows": rows,
        }
        write_json(args.output_dir / OUTPUT_NAMES[granularity], document)
        counts[granularity] = len(rows)

    write_json(
        args.output_dir / "cloud-usage-quality.json",
        {
            "schema_version": 1,
            "source": "claude_code_web",
            "audit": quality_rows[0],
            "cross_session_message_id_audit": cross_session_rows[0],
            "payload_timestamp_audit": payload_time_rows[0],
        },
    )
    write_json(
        args.output_dir / "cloud-usage-provider-model.json",
        {
            "schema_version": 1,
            "source": "claude_code_web",
            "provider_model_usage_metadata": provider_rows,
            "provider_mapping": {
                "anthropic": "result.modelUsage exact-model provider label firstParty, attributed to Anthropic from the Claude Code Web source context",
                "unknown": "no unambiguous firstParty label in exact-model result.modelUsage metadata",
            },
        },
    )
    global_metadata = global_metadata_rows[0]
    global_usage_rows.sort(key=lambda row: (row["bucket_start_utc"], row["provider"] or "", row["model"] or ""))
    for row in global_usage_rows:
        row.pop("record_type", None)
        row.pop("total_tokens_definition", None)
    write_json(
        args.output_dir / "cloud-usage-weekly-global.json",
        {
            "schema_version": 1,
            "source": "claude_code_web",
            "product_scope": "Claude Code Web archived sessions only; included subscription use versus overage or extra usage is unknown.",
            "granularity": "week",
            "database": global_metadata.get("database"),
            "extracted_at_utc": global_metadata.get("extracted_at_utc"),
            "source_window": global_metadata.get("source_window"),
            "deduplication": global_metadata.get("deduplication_scenario"),
            "weekly_time_assignment": global_metadata.get("weekly_time_assignment"),
            "token_estimate": global_metadata.get("token_estimate"),
            "payment_lane": "unknown",
            "token_definitions": {
                "uncached_input_tokens": "message.usage.input_tokens",
                "cache_read_input_tokens": "message.usage.cache_read_input_tokens",
                "cache_write_input_tokens": "message.usage.cache_creation_input_tokens",
                "output_tokens": "minimum last-sequence counter across session copies per global message ID; streaming and cross-copy min/max are retained as bounds",
                "reasoning_output_tokens": "message.usage.output_tokens_details.thinking_tokens when complete; subset of output_tokens, never add to totals",
                "total_tokens": "derived sum of uncached input + cache read + cache write + output; not provider-reported or billable",
            },
            "rows": global_usage_rows,
        },
    )
    print(json.dumps({"rows_by_granularity": counts, "metadata_keys": sorted(metadata)}))


if __name__ == "__main__":
    main()
