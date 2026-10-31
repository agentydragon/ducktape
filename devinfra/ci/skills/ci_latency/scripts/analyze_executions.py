#!/usr/bin/env python3
"""Summarize offline BuildBuddy SearchInvocation and GetExecution snapshots."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_SPAN_FIELDS = {
    "queue": ("queuedTimestamp", "workerStartTimestamp"),
    "worker_preparation": ("workerStartTimestamp", "executionStartTimestamp"),
}
_METADATA_TIMESTAMPS = (
    "queuedTimestamp",
    "workerStartTimestamp",
    "workerCompletedTimestamp",
    "inputFetchStartTimestamp",
    "inputFetchCompletedTimestamp",
    "executionStartTimestamp",
    "executionCompletedTimestamp",
    "outputUploadStartTimestamp",
    "outputUploadCompletedTimestamp",
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON from {path}: {error}") from error


def _as_records(value: Any, *, field: str, context: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{context}: {field} must be a list of objects")
    return list(value)


def _invocation_records(document: Any) -> list[Mapping[str, Any]]:
    """Read bbapi protobuf JSON or the paged SearchInvocation export wrapper."""
    if isinstance(document, list):
        return _as_records(document, field="invocation list", context="invocations")
    if not isinstance(document, Mapping):
        raise ValueError("invocations: expected an object or list")
    if not document:
        return []

    if "pages" in document:
        pages = _as_records(document["pages"], field="pages", context="invocations")
        records: list[Mapping[str, Any]] = []
        for page_number, page in enumerate(pages, 1):
            if "invocation" not in page:
                if not any(page.values()):
                    continue
                raise ValueError(f"invocations page {page_number}: missing 'invocation' list")
            records.extend(
                _as_records(page["invocation"], field="invocation", context=f"invocations page {page_number}")
            )
        return records
    if "invocation" in document:
        return _as_records(document["invocation"], field="invocation", context="invocations")
    if "invocations" in document:
        return _as_records(document["invocations"], field="invocations", context="invocations")
    raise ValueError("invocations: expected an invocation list or SearchInvocation pages")


def _execution_records(document: Any, *, context: str) -> list[Mapping[str, Any]]:
    """Read raw GetExecution JSON and the saved export wrapper used by bb-live."""
    if not isinstance(document, Mapping):
        raise ValueError(f"{context}: expected an object")
    response = document.get("response", document)
    if not isinstance(response, Mapping):
        raise ValueError(f"{context}: expected a GetExecution object")
    if "execution" not in response:
        if not response:
            return []
        raise ValueError(f"{context}: expected a GetExecution 'execution' list")
    return _as_records(response["execution"], field="execution", context=context)


def _expand_execution_paths(paths: Iterable[str]) -> list[Path]:
    files: set[Path] = set()
    for value in paths:
        path = Path(value)
        if path.is_dir():
            files.update(candidate.resolve() for candidate in path.rglob("*.json") if candidate.is_file())
        elif path.is_file():
            files.add(path.resolve())
        else:
            raise ValueError(f"execution input does not exist: {path}")
    if not files:
        raise ValueError("no GetExecution JSON files found")
    return sorted(files)


def _utc_observation(value: str) -> datetime:
    if not _RFC3339.fullmatch(value) or not value.endswith(("Z", "+00:00")):
        raise ValueError("--observed-at must be an RFC3339 UTC timestamp ending in Z or +00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid --observed-at timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("--observed-at must be an RFC3339 UTC timestamp ending in Z or +00:00")
    return parsed.astimezone(UTC)


def _clock(value: Any, observed_at: datetime) -> tuple[datetime | None, str | None]:
    if value is None or value == "":
        return None, "missing_timestamp"
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return None, "invalid_timestamp"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, "invalid_timestamp"
    if parsed.tzinfo is None:
        return None, "invalid_timestamp"
    parsed = parsed.astimezone(UTC)
    if parsed <= _EPOCH:
        return None, "epoch_timestamp"
    if parsed > observed_at:
        return None, "after_observed_at"
    return parsed, None


def _span(
    metadata: Mapping[str, Any], start_key: str, end_key: str, observed_at: datetime
) -> tuple[float | None, str | None]:
    start, start_error = _clock(metadata.get(start_key), observed_at)
    if start_error:
        return None, f"start_{start_error}"
    end, end_error = _clock(metadata.get(end_key), observed_at)
    if end_error:
        return None, f"end_{end_error}"
    assert start is not None
    assert end is not None
    if end < start:
        return None, "non_monotonic_timestamps"
    return (end - start).total_seconds(), None


def _execution_id(record: Mapping[str, Any], context: str) -> str:
    value = record.get("executionId")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}: execution record is missing executionId")
    return value


def _mnemonic(record: Mapping[str, Any]) -> str:
    value = record.get("actionMnemonic")
    return value if isinstance(value, str) and value else "(unknown)"


def _stage(record: Mapping[str, Any]) -> str:
    value = record.get("stage")
    return value if isinstance(value, str) and value else "(unknown)"


def _metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("executedActionMetadata")
    return value if isinstance(value, Mapping) else {}


def _record_rank(record: Mapping[str, Any], observed_at: datetime) -> tuple[int, int, int, str]:
    """Prefer a completed duplicate, then the copy with the most usable clocks."""
    metadata = _metadata(record)
    valid = sum(_clock(metadata.get(key), observed_at)[0] is not None for key in _METADATA_TIMESTAMPS)
    completion = sum(
        _clock(metadata.get(key), observed_at)[0] is not None
        for key in ("workerCompletedTimestamp", "executionCompletedTimestamp", "outputUploadCompletedTimestamp")
    )
    completed = int(_stage(record).upper() == "COMPLETED")
    return completed, completion, valid, json.dumps(record, sort_keys=True, separators=(",", ":"))


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _span_summary(values: list[float], unknown_reasons: Counter[str], execution_count: int) -> dict[str, Any]:
    return {
        "execution_count": execution_count,
        "known_count": len(values),
        "unknown_count": execution_count - len(values),
        "unknown_reasons": dict(sorted(unknown_reasons.items())),
        "sum_action_seconds": round(sum(values), 6) if values else None,
        "mean_seconds": round(sum(values) / len(values), 6) if values else None,
        "p50_seconds": _percentile(values, 0.50),
        "p90_seconds": _percentile(values, 0.90),
    }


def analyze(invocation_document: Any, execution_documents: Sequence[Any], observed_at_value: str) -> dict[str, Any]:
    observed_at = _utc_observation(observed_at_value)
    invocation_rows = _invocation_records(invocation_document)
    invocation_ids: set[str] = set()
    duplicate_invocation_rows = 0
    for row in invocation_rows:
        invocation_id = row.get("invocationId")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise ValueError("invocation record is missing invocationId")
        if invocation_id in invocation_ids:
            duplicate_invocation_rows += 1
        invocation_ids.add(invocation_id)

    records_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    row_count = 0
    for export_index, document in enumerate(execution_documents, 1):
        records = _execution_records(document, context=f"GetExecution export {export_index}")
        row_count += len(records)
        for record in records:
            execution_id = _execution_id(record, f"GetExecution export {export_index}")
            records_by_id[execution_id].append(record)

    unique_records: list[Mapping[str, Any]] = []
    for execution_id, copies in records_by_id.items():
        mnemonics = {_mnemonic(record) for record in copies}
        if len(mnemonics) > 1:
            raise ValueError(f"duplicate executionId has conflicting actionMnemonic: {execution_id}")
        unique_records.append(max(copies, key=lambda record: _record_rank(record, observed_at)))

    counts: Counter[tuple[str, str]] = Counter((_mnemonic(record), _stage(record)) for record in unique_records)
    by_mnemonic: Counter[str] = Counter(_mnemonic(record) for record in unique_records)
    span_values: dict[str, dict[str, list[float]]] = {span_name: defaultdict(list) for span_name in _SPAN_FIELDS}
    span_unknown: dict[str, dict[str, Counter[str]]] = {span_name: defaultdict(Counter) for span_name in _SPAN_FIELDS}
    for record in unique_records:
        mnemonic = _mnemonic(record)
        metadata = _metadata(record)
        for span_name, (start_key, end_key) in _SPAN_FIELDS.items():
            seconds, reason = _span(metadata, start_key, end_key, observed_at)
            if reason is None:
                assert seconds is not None
                span_values[span_name][mnemonic].append(seconds)
            else:
                span_unknown[span_name][mnemonic][reason] += 1

    stage_counts = [
        {"action_mnemonic": mnemonic, "stage": stage, "unique_execution_count": count}
        for (mnemonic, stage), count in sorted(counts.items())
    ]
    spans = {
        span_name: {
            mnemonic: _span_summary(span_values[span_name][mnemonic], span_unknown[span_name][mnemonic], count)
            for mnemonic, count in sorted(by_mnemonic.items())
        }
        for span_name in _SPAN_FIELDS
    }
    return {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "input": {
            "invocation_rows": len(invocation_rows),
            "unique_invocations": len(invocation_ids),
            "duplicate_invocation_rows": duplicate_invocation_rows,
            "execution_exports": len(execution_documents),
            "execution_rows": row_count,
            "unique_execution_ids": len(unique_records),
            "duplicate_execution_rows_removed": row_count - len(unique_records),
        },
        "stage_counts": stage_counts,
        "action_spans": spans,
        "interpretation": {
            "span_totals_are_action_seconds": True,
            "partial_rows_are_not_aged_from_observed_at": True,
            "worker_occupancy_inferred": False,
            "capacity_or_quota_inferred": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--invocations", required=True, help="bbapi invocation list --json or SearchInvocation export JSON"
    )
    parser.add_argument(
        "--executions", nargs="+", required=True, help="GetExecution JSON files or directories containing them"
    )
    parser.add_argument("--observed-at", required=True, help="RFC3339 UTC time when the exports were observed")
    args = parser.parse_args()

    try:
        invocation_document = _read_json(Path(args.invocations))
        execution_files = _expand_execution_paths(args.executions)
        execution_documents = [_read_json(path) for path in execution_files]
        result = analyze(invocation_document, execution_documents, args.observed_at)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
