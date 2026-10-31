"""Extract aggregate-only retained Langfuse token usage from ClickHouse.

The SQL runs in bounded UTC windows. Individual model-call records and IDs stay
inside ClickHouse; this program receives only five-minute grouped aggregates.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIVE_MINUTE_SQL = HERE / "langfuse-usage-five-minute.sql"
ID_AUDIT_SQL = HERE / "langfuse-usage-id-audit.sql"
DEFAULT_START = datetime(2026, 8, 30, tzinfo=UTC)
DEFAULT_END = datetime(2026, 11, 1, tzinfo=UTC)
DEFAULT_OUTPUT = HERE / "evidence"
DEFAULT_CHECKPOINT = Path("/tmp/ai-usage-investigation/langfuse-chunks")
CLICKHOUSE_COMMAND = [
    "kubectl",
    "exec",
    "-i",
    "-n",
    "clickhouse",
    "chi-clickhouse-default-0-0-0",
    "--",
    "clickhouse-client",
    "--readonly",
    "2",
    "--max_execution_time",
    "25",
    "--max_memory_usage",
    "536870912",
    "--max_block_size",
    "64",
    "--preferred_block_size_bytes",
    "1048576",
    "--preferred_max_column_in_block_size_bytes",
    "8388608",
]
DIMENSIONS = (
    "source",
    "ingestion_source",
    "schema_epoch",
    "provider",
    "provider_attribution_basis",
    "model",
    "canonical_model",
    "model_resolution_basis",
    "virtual_key_alias",
    "payment_lane",
    "account_scope",
    "route_observed",
    "provider_observed",
)
MODEL_ROUTE_PREFIXES = {
    "chatgpt-sub/oai-responses/": "current_litellm_route_config",
    "chatgpt-sub/ant-messages/": "current_litellm_route_config",
    "chatgpt/oai-responses/": "historical_litellm_route_config_at_9a9a38eb71_parent",
    "anthropic-max20/ant-messages/": "current_litellm_route_config",
    "anthropic-api/ant-messages/": "current_litellm_route_config",
    "antigravity/ant-messages/": "current_litellm_route_config",
}
COUNT_FIELDS = (
    "requests",
    "span_events_before_id_dedup",
    "duplicate_identifier_rows_deduped_within_bucket",
    "error_requests",
    "unresolved_model_requests",
    "missing_usage_requests",
    "missing_alias_requests",
    "with_litellm_call_id_requests",
    "distinct_litellm_call_ids",
    "distinct_litellm_call_ids_without_response_id",
    "with_provider_response_id_requests",
    "distinct_provider_response_ids",
    "no_explicit_id_requests",
    "traces",
    "input_known_requests",
    "input_positive_cache_requests",
    "input_total_inclusive_requests",
    "input_total_exclusive_requests",
    "input_total_algebra_unknown_requests",
    "input_cache_unreported_requests",
    "cache_read_known_requests",
    "cache_write_known_requests",
    "output_known_requests",
    "reasoning_known_requests",
    "total_known_requests",
    "input_inclusivity_unknown_requests",
    "reasoning_positive_requests",
    "reasoning_total_additive_requests",
    "reasoning_total_subset_requests",
    "reasoning_total_algebra_unknown_requests",
    "both_reasoning_keys_requests",
    "conflicting_reasoning_keys_requests",
    "usage_maps_equal_requests",
)
NUMERIC_FIELDS = (
    "gen_ai_input_attr_sum",
    "gen_ai_output_attr_sum",
    "gen_ai_total_attr_sum",
    "gen_ai_cache_read_attr_sum",
    "gen_ai_cache_write_attr_sum",
    "llm_prompt_attr_sum",
    "llm_completion_attr_sum",
    "llm_reasoning_attr_sum",
    "llm_cache_read_attr_sum",
)
NORMALIZED_FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "normalized_category_sum_tokens",
    "total_reconciliation_delta",
)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include Z or a UTC offset")
    return parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_for_window(template: str, start: datetime, end: datetime) -> str:
    return template.replace("DATE_START_UTC", start.strftime("%Y-%m-%d %H:%M:%S")).replace(
        "DATE_END_UTC", end.strftime("%Y-%m-%d %H:%M:%S")
    )


def checkpoint_path(checkpoint_dir: Path, start: datetime, end: datetime) -> Path:
    return checkpoint_dir / f"{start:%Y%m%dT%H%M%SZ}--{end:%Y%m%dT%H%M%SZ}.json.gz"


def read_checkpoint(path: Path) -> tuple[datetime, datetime, list[dict[str, Any]]]:
    with gzip.open(path, "rt", encoding="utf-8") as checkpoint_file:
        value = json.load(checkpoint_file)
    return parse_utc(value["window_start_utc"]), parse_utc(value["window_end_utc"]), value["rows"]


def checkpoint_chunks(
    checkpoint_dir: Path, start: datetime, end: datetime
) -> list[tuple[datetime, datetime, list[dict[str, Any]]]]:
    chunks = []
    for path in checkpoint_dir.glob("*.json.gz"):
        chunk_start, chunk_end, rows = read_checkpoint(path)
        if chunk_start >= start and chunk_end <= end:
            chunks.append((chunk_start, chunk_end, rows))
    chunks.sort(key=lambda chunk: chunk[0])
    return chunks


def write_checkpoint(checkpoint_dir: Path, start: datetime, end: datetime, rows: list[dict[str, Any]]) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_path(checkpoint_dir, start, end)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as checkpoint_file:
        json.dump(
            {"window_start_utc": iso_utc(start), "window_end_utc": iso_utc(end), "rows": rows},
            checkpoint_file,
            separators=(",", ":"),
        )
    temporary.replace(path)


def run_query(
    sql: str,
    *,
    template: str,
    window_start: datetime,
    window_end: datetime,
    min_hours: float = 1,
    max_execution_seconds: int = 60,
    checkpoint_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    if checkpoint_dir is not None:
        chunks = checkpoint_chunks(checkpoint_dir, window_start, window_end)
        if chunks:
            combined: list[dict[str, Any]] = []
            cursor = window_start
            query_seconds = 0
            query_windows = 0
            for chunk_start, chunk_end, rows in chunks:
                if chunk_start < cursor:
                    raise RuntimeError("overlapping aggregate checkpoints; refusing to double-count usage")
                if chunk_start > cursor:
                    gap_rows, elapsed, windows = run_query(
                        query_for_window(template, cursor, chunk_start),
                        template=template,
                        window_start=cursor,
                        window_end=chunk_start,
                        min_hours=min_hours,
                        max_execution_seconds=max_execution_seconds,
                        checkpoint_dir=checkpoint_dir,
                    )
                    combined.extend(gap_rows)
                    query_seconds += elapsed
                    query_windows += windows
                combined.extend(rows)
                cursor = chunk_end
            if cursor < window_end:
                gap_rows, elapsed, windows = run_query(
                    query_for_window(template, cursor, window_end),
                    template=template,
                    window_start=cursor,
                    window_end=window_end,
                    min_hours=min_hours,
                    max_execution_seconds=max_execution_seconds,
                    checkpoint_dir=checkpoint_dir,
                )
                combined.extend(gap_rows)
                query_seconds += elapsed
                query_windows += windows
            return combined, query_seconds, query_windows + len(chunks)

    started = time.monotonic()
    command = CLICKHOUSE_COMMAND.copy()
    command[command.index("25")] = str(max_execution_seconds)
    try:
        result = subprocess.run(
            command, input=sql, text=True, capture_output=True, timeout=max_execution_seconds + 20, check=False
        )
    except subprocess.TimeoutExpired:
        result = None

    if result is not None and result.returncode == 0:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if checkpoint_dir is not None:
            write_checkpoint(checkpoint_dir, window_start, window_end, rows)
        return rows, round(time.monotonic() - started), 1

    elapsed = round(time.monotonic() - started)
    retryable = result is None or any(
        marker in result.stderr for marker in ("TIMEOUT_EXCEEDED", "MEMORY_LIMIT_EXCEEDED")
    )
    if retryable and (window_end - window_start).total_seconds() / 3600 > min_hours:
        failure_kind = (
            "client_timeout"
            if result is None
            else next(
                (marker for marker in ("MEMORY_LIMIT_EXCEEDED", "TIMEOUT_EXCEEDED") if marker in result.stderr),
                "retryable_clickhouse_failure",
            )
        )
        midpoint = window_start + (window_end - window_start) / 2
        midpoint = midpoint.replace(minute=0, second=0, microsecond=0)
        if midpoint <= window_start or midpoint >= window_end:
            midpoint = window_start + timedelta(minutes=30)
        print(
            f"splitting {failure_kind} {iso_utc(window_start)}..{iso_utc(window_end)} into smaller UTC windows ({elapsed}s)",
            file=sys.stderr,
            flush=True,
        )
        left, left_seconds, left_windows = run_query(
            query_for_window(template, window_start, midpoint),
            template=template,
            window_start=window_start,
            window_end=midpoint,
            min_hours=min_hours,
            max_execution_seconds=max_execution_seconds,
            checkpoint_dir=checkpoint_dir,
        )
        right, right_seconds, right_windows = run_query(
            query_for_window(template, midpoint, window_end),
            template=template,
            window_start=midpoint,
            window_end=window_end,
            min_hours=min_hours,
            max_execution_seconds=max_execution_seconds,
            checkpoint_dir=checkpoint_dir,
        )
        return left + right, left_seconds + right_seconds, left_windows + right_windows

    if result is None:
        raise RuntimeError(
            f"ClickHouse query timed out at minimum window {iso_utc(window_start)}..{iso_utc(window_end)}"
        )
    detail = next(
        (line.strip() for line in result.stderr.splitlines() if "DB::Exception" in line), "ClickHouse query failed"
    )
    raise RuntimeError(f"{detail}; minimum window {iso_utc(window_start)}..{iso_utc(window_end)}")


def int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def add_maps(target: dict[str, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        target[key] = target.get(key, 0) + int_or_zero(value)


def resolve_model(model: Any) -> tuple[str | None, str]:
    if not model:
        return None, "model_unreported"
    model = str(model)
    for prefix, source in MODEL_ROUTE_PREFIXES.items():
        if model.startswith(prefix):
            return model.removeprefix(prefix), source
    return model, "reported_model_preserved_unmapped"


def resolve_provider(row: dict[str, Any]) -> tuple[str, str]:
    lane = row.get("payment_lane")
    if lane == "openai_subscription_via_cli_proxy_oauth":
        return "openai", "explicit_openai_subscription_route"
    if lane in ("anthropic_subscription_via_cli_proxy_oauth", "anthropic_direct_api_key"):
        return "anthropic", "explicit_anthropic_route"
    model = str(row.get("model") or "").lower()
    if model.startswith(("openai/", "chatgpt/", "chatgpt-sub/")):
        return "openai", "reported_openai_route_prefix"
    if model.startswith(("anthropic/", "anthropic-api/", "anthropic-max20/")):
        return "anthropic", "reported_anthropic_route_prefix"
    if model.startswith(("gpt-", "o1-", "o1/", "o3-", "o3/", "o4-", "o4/")):
        return "openai", "reported_openai_model_family"
    if model.startswith("claude-"):
        return "anthropic", "reported_anthropic_model_family"
    return "unknown", "manufacturer_unresolved_provider_observed_not_authoritative"


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_usage_sums") or {}
    raw_provided = row.get("raw_provided_usage_sums") or {}
    reasoning_keys = [
        int_or_zero(raw.get("completion_details.reasoning")),
        int_or_zero(raw.get("output_reasoning_tokens")),
    ]
    reasoning = (
        max(reasoning_keys)
        if any(key in raw for key in ("completion_details.reasoning", "output_reasoning_tokens"))
        else None
    )
    if reasoning is None and int_or_zero(row.get("llm_reasoning_attr_sum")):
        reasoning = int_or_zero(row["llm_reasoning_attr_sum"])

    raw_input = int_or_zero(raw.get("input")) if row.get("input_known_requests", 0) else None
    if "input_cached_tokens" in raw:
        cache_read = int_or_zero(raw["input_cached_tokens"])
        cache_read_source = "usage_details.input_cached_tokens"
    elif row.get("cache_read_known_requests", 0):
        cache_read = int_or_zero(row.get("gen_ai_cache_read_attr_sum")) or int_or_zero(
            row.get("llm_cache_read_attr_sum")
        )
        cache_read_source = "metadata_attribute_fallback"
    else:
        cache_read = None
        cache_read_source = "unreported"

    if "input_cache_creation" in raw:
        cache_write = int_or_zero(raw["input_cache_creation"])
        cache_write_source = "usage_details.input_cache_creation"
    elif row.get("cache_write_known_requests", 0):
        cache_write = int_or_zero(row.get("gen_ai_cache_write_attr_sum"))
        cache_write_source = "metadata_attribute_fallback"
    else:
        cache_write = None
        cache_write_source = "unreported"

    positive_cache_requests = int_or_zero(row.get("input_positive_cache_requests"))
    inclusive_input_requests = int_or_zero(row.get("input_total_inclusive_requests"))
    exclusive_input_requests = int_or_zero(row.get("input_total_exclusive_requests"))
    input_algebra_unknown_requests = int_or_zero(row.get("input_total_algebra_unknown_requests"))
    input_cache_unreported_requests = int_or_zero(row.get("input_cache_unreported_requests"))
    input_inclusivity_unknown_requests = input_cache_unreported_requests + input_algebra_unknown_requests
    input_inclusivity_unknown_requests += max(
        0,
        positive_cache_requests - inclusive_input_requests - exclusive_input_requests - input_algebra_unknown_requests,
    )
    if inclusive_input_requests and exclusive_input_requests:
        input_inclusivity_unknown_requests += positive_cache_requests
    if raw_input is None:
        input_tokens = None
        input_basis = "input_unreported"
    elif "input_positive_cache_requests" not in row:
        input_tokens = None
        input_basis = "per_call_total_algebra_unavailable_in_retained_checkpoint"
        input_inclusivity_unknown_requests = int_or_zero(row.get("input_known_requests"))
    elif input_cache_unreported_requests:
        input_tokens = None
        input_basis = "cache_categories_unreported_input_inclusivity_unknown"
    elif positive_cache_requests == 0:
        input_tokens = raw_input
        input_basis = "raw_input_no_positive_cache_reported"
    elif (
        inclusive_input_requests == positive_cache_requests
        and exclusive_input_requests == 0
        and input_algebra_unknown_requests == 0
    ):
        input_tokens = max(0, raw_input - int_or_zero(cache_read) - int_or_zero(cache_write))
        input_basis = "per_call_total_proves_input_includes_cache"
    elif (
        exclusive_input_requests == positive_cache_requests
        and inclusive_input_requests == 0
        and input_algebra_unknown_requests == 0
    ):
        input_tokens = raw_input
        input_basis = "per_call_total_proves_input_excludes_cache"
    else:
        input_tokens = None
        input_basis = "per_call_total_does_not_resolve_input_inclusivity"

    output_base = int_or_zero(raw.get("output")) if row.get("output_known_requests", 0) else None
    if "total" in raw:
        total_tokens = int_or_zero(raw["total"])
        total_source = "usage_details.total"
    elif row.get("total_known_requests", 0):
        total_tokens = int_or_zero(row.get("gen_ai_total_attr_sum"))
        total_source = "metadata_attribute_fallback"
    else:
        total_tokens = None
        total_source = "unreported"

    reasoning_additive_requests = int_or_zero(row.get("reasoning_total_additive_requests"))
    reasoning_subset_requests = int_or_zero(row.get("reasoning_total_subset_requests"))
    reasoning_unknown_requests = int_or_zero(row.get("reasoning_total_algebra_unknown_requests"))
    reasoning_positive_requests = int_or_zero(row.get("reasoning_positive_requests"))
    reasoning_relation = "none_reported"
    if output_base is None:
        output_tokens = None
        if reasoning is not None:
            reasoning_relation = "reasoning_reported_without_output_base"
    elif reasoning_positive_requests == 0:
        output_tokens = output_base
        reasoning_relation = "zero_or_unreported_reasoning"
    elif reasoning_additive_requests and not reasoning_subset_requests and not reasoning_unknown_requests:
        output_tokens = output_base + int_or_zero(reasoning)
        reasoning_relation = "total_proves_reasoning_additive"
    elif reasoning_subset_requests and not reasoning_additive_requests and not reasoning_unknown_requests:
        output_tokens = output_base
        reasoning_relation = "total_proves_reasoning_included_in_output"
    else:
        output_tokens = None
        reasoning_relation = "reasoning_total_algebra_ambiguous"

    category_values = (input_tokens, cache_read, cache_write, output_tokens)
    category_sum = (
        sum(value for value in category_values if value is not None)
        if all(value is not None for value in category_values)
        else None
    )
    total_delta = category_sum - total_tokens if category_sum is not None and total_tokens is not None else None
    raw_reasoning_conflict = (
        "completion_details.reasoning" in raw
        and "output_reasoning_tokens" in raw
        and raw["completion_details.reasoning"] != raw["output_reasoning_tokens"]
    )
    span_events = int_or_zero(row.pop("span_events_before_id_dedup", row.pop("span_requests_after_final", 0)))
    dedup_requests = (
        int_or_zero(row.get("no_explicit_id_requests"))
        + int_or_zero(row.get("distinct_provider_response_ids"))
        + int_or_zero(row.get("distinct_litellm_call_ids_without_response_id"))
    )
    row.update(
        {
            "requests": dedup_requests,
            "span_events_before_id_dedup": span_events,
            "duplicate_identifier_rows_deduped_within_bucket": max(0, span_events - dedup_requests),
            "uncached_input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning,
            "total_tokens": total_tokens,
            "normalized_category_sum_tokens": category_sum,
            "total_reconciliation_delta": total_delta,
            "cache_read_normalization_source": cache_read_source,
            "cache_write_normalization_source": cache_write_source,
            "total_token_source": total_source,
            "input_inclusivity_basis": input_basis,
            "input_inclusivity_unknown_requests": input_inclusivity_unknown_requests,
            "reasoning_total_relation": reasoning_relation,
            "reasoning_key_conflict": raw_reasoning_conflict,
            "canonical_model": resolve_model(row.get("model"))[0],
            "model_resolution_basis": resolve_model(row.get("model"))[1],
            "token_normalization_basis": (
                "usage_details_with_metadata_attribute_fallback"
                if "metadata_attribute_fallback" in (cache_read_source, cache_write_source, total_source)
                else "langfuse_usage_details"
            ),
            "raw_usage_sums": raw,
            "raw_provided_usage_sums": raw_provided,
        }
    )
    row["provider"], row["provider_attribution_basis"] = resolve_provider(row)
    row["missing_usage_requests"] = int_or_zero(row.get("missing_usage_requests"))
    row["source_span_events"] = span_events
    return row


def aggregate_records(rows: list[dict[str, Any]], bucket_fn) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for source_row in rows:
        bucket = bucket_fn(source_row["bucket_start_utc"])
        key = (bucket, *(source_row.get(name) for name in DIMENSIONS))
        target = groups.get(key)
        if target is None:
            target = {"bucket_start_utc": bucket}
            target.update({name: source_row.get(name) for name in DIMENSIONS})
            target.update(dict.fromkeys(COUNT_FIELDS + NUMERIC_FIELDS, 0))
            target.update(dict.fromkeys(NORMALIZED_FIELDS, 0))
            target.update(
                {
                    "raw_usage_sums": {},
                    "raw_provided_usage_sums": {},
                    "normalization_bases": set(),
                    "cache_read_sources": set(),
                    "cache_write_sources": set(),
                    "total_token_sources": set(),
                    "input_inclusivity_bases": set(),
                    "normalized_fields_present": set(),
                    "normalized_field_nulls": dict.fromkeys(NORMALIZED_FIELDS, 0),
                    "reasoning_key_conflict_rows": 0,
                }
            )
            groups[key] = target

        for name in COUNT_FIELDS + NUMERIC_FIELDS:
            target[name] += int_or_zero(source_row.get(name))
        for name in NORMALIZED_FIELDS:
            value = source_row.get(name)
            if value is not None:
                target[name] += int(value)
                target["normalized_fields_present"].add(name)
            else:
                target["normalized_field_nulls"][name] += 1
        add_maps(target["raw_usage_sums"], source_row.get("raw_usage_sums"))
        add_maps(target["raw_provided_usage_sums"], source_row.get("raw_provided_usage_sums"))
        target["normalization_bases"].add(source_row.get("token_normalization_basis", "unknown"))
        target["cache_read_sources"].add(source_row.get("cache_read_normalization_source", "unknown"))
        target["cache_write_sources"].add(source_row.get("cache_write_normalization_source", "unknown"))
        target["total_token_sources"].add(source_row.get("total_token_source", "unknown"))
        target["input_inclusivity_bases"].add(source_row.get("input_inclusivity_basis", "unknown"))
        target["reasoning_key_conflict_rows"] += int(bool(source_row.get("reasoning_key_conflict")))

    result = []
    for record in groups.values():
        for name in NORMALIZED_FIELDS:
            if name not in record["normalized_fields_present"] or record["normalized_field_nulls"][name]:
                record[name] = None
        record.pop("normalized_fields_present")
        record["normalized_field_nulls"] = {
            name: int(value) for name, value in record.pop("normalized_field_nulls").items() if value
        }
        record["token_normalization_basis"] = ",".join(sorted(record.pop("normalization_bases")))
        record["cache_read_normalization_source"] = ",".join(sorted(record.pop("cache_read_sources")))
        record["cache_write_normalization_source"] = ",".join(sorted(record.pop("cache_write_sources")))
        record["total_token_source"] = ",".join(sorted(record.pop("total_token_sources")))
        record["input_inclusivity_basis"] = ",".join(sorted(record.pop("input_inclusivity_bases")))
        record["reasoning_key_conflict_rows"] = int(record["reasoning_key_conflict_rows"])
        reasoning_positive = record["reasoning_positive_requests"]
        reasoning_additive = record["reasoning_total_additive_requests"]
        reasoning_subset = record["reasoning_total_subset_requests"]
        reasoning_unknown = record["reasoning_total_algebra_unknown_requests"]
        if not reasoning_positive:
            record["reasoning_total_relation"] = "none_reported"
        elif reasoning_additive == reasoning_positive and not reasoning_subset and not reasoning_unknown:
            record["reasoning_total_relation"] = "total_proves_reasoning_additive"
        elif reasoning_subset == reasoning_positive and not reasoning_additive and not reasoning_unknown:
            record["reasoning_total_relation"] = "total_proves_reasoning_included_in_output"
        else:
            record["reasoning_total_relation"] = "reasoning_total_algebra_ambiguous"
        record["source_span_events"] = record["span_events_before_id_dedup"]
        result.append(record)
    result.sort(
        key=lambda item: (
            item["bucket_start_utc"],
            str(item.get("provider")),
            str(item.get("model")),
            str(item.get("payment_lane")),
            str(item.get("virtual_key_alias")),
        )
    )
    return result


def parse_bucket(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def hourly_bucket(value: str) -> str:
    parsed = parse_bucket(value).replace(minute=0, second=0, microsecond=0)
    return iso_utc(parsed)


def weekly_bucket(value: str) -> str:
    parsed = parse_bucket(value).replace(hour=0, minute=0, second=0, microsecond=0)
    parsed -= timedelta(days=parsed.weekday())
    return iso_utc(parsed)


def schema_epoch_input_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    audit: dict[str, dict[str, Any]] = {}
    for row in rows:
        epoch = str(row.get("schema_epoch") or "unknown")
        entry = audit.setdefault(
            epoch,
            {
                "requests": 0,
                "input_known_requests": 0,
                "input_inclusivity_unknown_requests": 0,
                "input_inclusivity_basis_requests": {},
            },
        )
        requests = int_or_zero(row.get("requests"))
        entry["requests"] += requests
        entry["input_known_requests"] += int_or_zero(row.get("input_known_requests"))
        entry["input_inclusivity_unknown_requests"] += int_or_zero(row.get("input_inclusivity_unknown_requests"))
        basis = str(row.get("input_inclusivity_basis") or "unknown")
        bases: dict[str, int] = entry["input_inclusivity_basis_requests"]
        bases[basis] = bases.get(basis, 0) + requests
    return audit


def provider_attribution_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    audit: dict[str, int] = {}
    for row in rows:
        key = f"{row.get('provider', 'unknown')}:{row.get('provider_attribution_basis', 'unknown')}"
        audit[key] = audit.get(key, 0) + int_or_zero(row.get("requests"))
    return audit


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_json_gzip(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as compressed:
        compressed.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", type=parse_utc, default=DEFAULT_START, help="inclusive UTC start, e.g. 2026-08-30T00:00:00Z"
    )
    parser.add_argument(
        "--end", type=parse_utc, default=DEFAULT_END, help="exclusive UTC end, e.g. 2026-11-01T00:00:00Z"
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=12,
        help="initial bounded query window; failures split in half down to 1 hour",
    )
    parser.add_argument(
        "--max-execution-seconds",
        type=int,
        default=60,
        help="bounded ClickHouse query timeout before splitting the window",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument(
        "--skip-identifier-audit",
        action="store_true",
        help="write an explicit incomplete audit envelope when the global ClickHouse collision query is too costly",
    )
    args = parser.parse_args()
    if args.end <= args.start:
        parser.error("--end must be after --start")

    template = FIVE_MINUTE_SQL.read_text()
    checkpoint_dir = args.checkpoint_dir
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    raw_rows: list[dict[str, Any]] = []
    window = timedelta(hours=args.window_hours)
    cursor = args.start
    query_seconds = 0
    window_index = 0
    while cursor < args.end:
        stop = min(cursor + window, args.end)
        statement = query_for_window(template, cursor, stop)
        chunk, elapsed, actual_windows = run_query(
            statement,
            template=template,
            window_start=cursor,
            window_end=stop,
            max_execution_seconds=args.max_execution_seconds,
            checkpoint_dir=checkpoint_dir,
        )
        window_index += actual_windows
        raw_rows.extend(chunk)
        query_seconds += elapsed
        print(
            f"window {window_index}: {iso_utc(cursor)}..{iso_utc(stop)} rows={len(chunk)} query_seconds={elapsed}",
            file=sys.stderr,
            flush=True,
        )
        cursor = stop

    normalized_rows = [normalize_row(row) for row in raw_rows]
    input_audit_by_schema_epoch = schema_epoch_input_audit(normalized_rows)
    provider_audit = provider_attribution_audit(normalized_rows)
    bucket_starts = [parse_bucket(row["bucket_start_utc"]) for row in normalized_rows]
    observed_start = iso_utc(min(bucket_starts)) if bucket_starts else None
    observed_end = iso_utc(max(bucket_starts) + timedelta(minutes=5)) if bucket_starts else None
    five_minute = {
        "schema_version": 1,
        "source": "langfuse_clickhouse",
        "generated_at_utc": iso_utc(datetime.now(UTC)),
        "window_start_utc": iso_utc(args.start),
        "window_end_utc": iso_utc(args.end),
        "observed_watermark_start_utc": observed_start,
        "observed_watermark_end_exclusive_utc": observed_end,
        "bucket_timezone": "UTC",
        "bucket_width": "5 minutes",
        "week_start": "Monday UTC",
        "query_windows": window_index,
        "query_seconds_rounded_sum": query_seconds,
        "max_execution_seconds_per_query": args.max_execution_seconds,
        "max_memory_usage_bytes_per_query": 536870912,
        "max_block_size_per_query": 64,
        "preferred_block_size_bytes_per_query": 1048576,
        "preferred_max_column_in_block_size_bytes_per_query": 8388608,
        "request_count_basis": "GENERATION model-call spans after events_full FINAL, collapsed inside each bounded query window by project plus provider response ID, otherwise LiteLLM call ID, otherwise span ID; one representative span contributes usage per identity. Aggregate identifier audit checks whether IDs repeat across query-window boundaries.",
        "request_dedup_scope": {
            "bounded_query_windows": window_index,
            "within_window_dedup_applied": True,
            "cross_window_collision_status": "unverified_global_audit_memory_limit"
            if args.skip_identifier_audit
            else "audited",
        },
        "definitions": {
            "uncached_input_tokens": "Per-call input is reduced by cache-read and cache-write tokens only when per-call totals prove input includes cache; input is retained only when totals prove it excludes cache or no positive cache is reported and cache categories are reported. Unknown, mixed, or unreported cache/inclusivity groups are null and flagged. Provider labels do not determine inclusivity.",
            "cache_read_input_tokens": "usage_details.input_cached_tokens, falling back to gen_ai.usage.cache_read.input_tokens or llm.token_count.prompt_details.cache_read only when the usage map has no cache-read bucket; fallback source is identified per row.",
            "cache_write_input_tokens": "usage_details.input_cache_creation, falling back to gen_ai.usage.cache_creation.input_tokens only when the usage map has no cache-write bucket.",
            "input_total_inclusive_requests": "Deduplicated calls with positive cache where source total reconciles to input + output, optionally plus reasoning; this proves reported input already includes cache for this adapter group.",
            "input_total_exclusive_requests": "Deduplicated calls with positive cache where source total reconciles to input + cache-read + cache-write + output, optionally plus reasoning; this proves reported input excludes separately reported cache.",
            "input_total_algebra_unknown_requests": "Calls with positive cache where source total is absent or matches neither inclusive nor exclusive input algebra.",
            "output_tokens": "usage_details.output plus reasoning only when per-call usage_details.total reconciles to input/cache/output/reasoning as a separate bucket; if total reconciles without reasoning, reasoning is treated as included and not added; inconclusive groups are null and flagged.",
            "reasoning_output_tokens": "The maximum of mutually named Langfuse reasoning keys per row to avoid double-counting overlapping representations; disagreements are flagged.",
            "total_tokens": "Source-provided usage_details.total when present, otherwise gen_ai.usage.total_tokens; not recomputed from side-channel categories. normalized_category_sum_tokens is separately emitted for reconciliation.",
            "requests": "Within-group deduplicated actual calls: unique provider response IDs take precedence, then unique LiteLLM call IDs, then one request per finalized span with neither ID. The separate identifier audit detects collisions across output groups and gives their token sensitivity.",
        },
        "input_inclusivity_audit_by_schema_epoch": input_audit_by_schema_epoch,
        "provider_attribution_audit": provider_audit,
        "rows": normalized_rows,
    }
    hourly_rows = aggregate_records(normalized_rows, hourly_bucket)
    weekly_rows = aggregate_records(normalized_rows, weekly_bucket)
    common = {key: value for key, value in five_minute.items() if key != "rows"}
    write_json_gzip(args.output_dir / "langfuse-usage-five-minute.json.gz", five_minute)
    write_json_gzip(
        args.output_dir / "langfuse-usage-hourly.json.gz", {**common, "bucket_width": "1 hour", "rows": hourly_rows}
    )
    write_json(
        args.output_dir / "langfuse-usage-weekly.json", {**common, "bucket_width": "1 week", "rows": weekly_rows}
    )
    print(
        f"wrote {len(normalized_rows)} five-minute, {len(hourly_rows)} hourly, {len(weekly_rows)} weekly aggregate rows",
        file=sys.stderr,
    )

    audit_template = ID_AUDIT_SQL.read_text()
    audit_rows: list[dict[str, Any]]
    if args.skip_identifier_audit:
        audit_rows, audit_seconds, audit_windows = [], 0, 0
    else:
        audit_rows, audit_seconds, audit_windows = run_query(
            query_for_window(audit_template, args.start, args.end),
            template=audit_template,
            window_start=args.start,
            window_end=args.end,
            max_execution_seconds=120,
        )
    id_audit = {
        "schema_version": 1,
        "source": "langfuse_clickhouse",
        "generated_at_utc": iso_utc(datetime.now(UTC)),
        "window_start_utc": iso_utc(args.start),
        "window_end_utc": iso_utc(args.end),
        "query_seconds_rounded_sum": audit_seconds,
        "query_windows": audit_windows,
        "audit_performed": not args.skip_identifier_audit,
        "audit_status": "unverified_global_query_memory_limited" if args.skip_identifier_audit else "completed",
        "audit_limitation": "Global ID collision query hit the 512 MiB ClickHouse MergeTreeSelect metadata_values reader limit at a 1-hour window; within-query dedup remains applied, cross-window collisions are unverified."
        if args.skip_identifier_audit
        else None,
        "global_identifier_dedup_exact": audit_windows == 1,
        "identifiers_exported": False,
        "definitions": {
            "colliding_identifier_groups": "Explicit LiteLLM call IDs or provider response IDs observed on more than one finalized generation span within one Langfuse project.",
            "duplicate_candidate_event_rows": "Rows beyond one event per explicit identifier; candidates only because duplicate-looking IDs could still represent distinct provider calls.",
            "duplicate_*_token_sensitivity": "For each collision group, summed token categories minus the largest single-row category; this is candidate dedup sensitivity, not silently applied to totals.",
            "windowed_limit": "If query_windows is greater than one, duplicate IDs spanning different audit windows cannot be detected by this aggregate-only query.",
        },
        "rows": audit_rows,
    }
    call_id_duplicates_without_response = (
        None
        if args.skip_identifier_audit
        else sum(int_or_zero(row.get("duplicate_call_id_rows_without_response_id")) for row in audit_rows)
    )
    response_id_duplicate_rows = (
        None
        if args.skip_identifier_audit
        else sum(
            int_or_zero(row.get("duplicate_candidate_event_rows"))
            for row in audit_rows
            if row.get("identifier_kind") == "provider_response_id"
        )
    )
    local_duplicate_rows = sum(
        int_or_zero(row.get("duplicate_identifier_rows_deduped_within_bucket")) for row in normalized_rows
    )
    dedup_verified = (
        audit_windows == 1
        and response_id_duplicate_rows == 0
        and local_duplicate_rows == call_id_duplicates_without_response
    )
    dedup_audit = {
        "identifier_audit_windows": audit_windows,
        "global_identifier_audit_exact": audit_windows == 1,
        "provider_response_id_duplicate_candidate_rows": response_id_duplicate_rows,
        "call_id_duplicate_rows_without_response_id": call_id_duplicates_without_response,
        "duplicate_rows_already_removed_within_5m_dimensions": local_duplicate_rows,
        "unlocalized_call_id_duplicate_rows": (
            None
            if call_id_duplicates_without_response is None
            else call_id_duplicates_without_response - local_duplicate_rows
        ),
        "request_dedup_verified_exact": dedup_verified,
        "request_dedup_status": "verified_exact_for_window"
        if dedup_verified
        else "global_identifier_audit_memory_limited_cross_window_unverified"
        if args.skip_identifier_audit
        else "candidate_rows_or_unlocalized_ID_collisions_remain",
    }
    five_minute["request_dedup_audit"] = dedup_audit
    id_audit["request_dedup_audit"] = dedup_audit
    common = {key: value for key, value in five_minute.items() if key != "rows"}
    write_json_gzip(args.output_dir / "langfuse-usage-five-minute.json.gz", five_minute)
    write_json_gzip(
        args.output_dir / "langfuse-usage-hourly.json.gz", {**common, "bucket_width": "1 hour", "rows": hourly_rows}
    )
    write_json(
        args.output_dir / "langfuse-usage-weekly.json", {**common, "bucket_width": "1 week", "rows": weekly_rows}
    )
    write_json(args.output_dir / "langfuse-usage-identifier-audit.json", id_audit)
    shutil.rmtree(checkpoint_dir)
    print(f"wrote aggregate identifier audit using {audit_windows} query window(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
