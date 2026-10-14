"""Summarize raw timing, token, score, and memory evidence for one frozen cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(row)
    return rows


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _distribution(values: list[float], unit: str) -> dict[str, float | int | str | None]:
    if not values:
        return {"samples": 0, "median": None, "min": None, "max": None, "unit": unit}
    return {
        "samples": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "unit": unit,
    }


def _metric(row: dict[str, Any]) -> dict[str, float | None]:
    native = row.get("native_metrics")
    native = native if isinstance(native, dict) else {}
    wall = _number(row.get("wall_seconds"))
    if wall is None:
        wall = _number(row.get("elapsed_seconds"))

    prompt_count = _number(native.get("prompt_eval_count"))
    if prompt_count is None:
        prompt_count = _number(row.get("input_tokens"))
    output_count = _number(native.get("eval_count"))
    if output_count is None:
        output_count = _number(row.get("generated_tokens"))

    prompt_duration = _number(native.get("prompt_eval_duration"))
    prefill_seconds = prompt_duration / 1e9 if prompt_duration is not None else None
    eval_duration = _number(native.get("eval_duration"))
    decode_seconds = eval_duration / 1e9 if eval_duration is not None else None
    if decode_seconds is None:
        reported_tps = _number(row.get("throughput_tps"))
        if reported_tps is not None and reported_tps > 0 and output_count is not None:
            decode_seconds = output_count / reported_tps

    ttft_ms = _number(row.get("ttft_ms"))
    return {
        "wall_seconds": wall,
        "prefill_seconds": prefill_seconds,
        "decode_seconds": decode_seconds,
        "input_tokens": prompt_count,
        "output_tokens": output_count,
        "decode_tokens_per_second": (
            output_count / decode_seconds if output_count is not None and decode_seconds else None
        ),
        "time_to_first_token_ms": ttft_ms,
    }


def _summarize_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measures: dict[str, list[float]] = {
        "wall_seconds": [],
        "prefill_seconds": [],
        "decode_seconds": [],
        "input_tokens": [],
        "output_tokens": [],
        "decode_tokens_per_second": [],
        "time_to_first_token_ms": [],
    }
    for row in rows:
        for key, value in _metric(row).items():
            if value is not None:
                measures[key].append(value)
    return {
        key: _distribution(
            values,
            "ms"
            if key.endswith("_ms")
            else "tokens"
            if key.endswith("_tokens")
            else "seconds"
            if key.endswith("_seconds")
            else "tokens/s",
        )
        for key, values in measures.items()
    }


def _status(row: dict[str, Any]) -> str:
    status = row.get("status")
    if isinstance(status, str):
        return status
    if row.get("response") is not None and _metric(row)["wall_seconds"] is not None:
        return "ok"
    return "missing_or_error"


def _score_summary(scores_path: Path | None, score_summary_path: Path | None) -> dict[str, Any] | None:
    quality: dict[str, Any] = {}
    if score_summary_path is not None:
        raw_summary = json.loads(score_summary_path.read_text())
        if not isinstance(raw_summary, dict):
            raise ValueError("score summary must be a JSON object")
        for key in (
            "candidate",
            "tasks",
            "repeats_per_task",
            "samples",
            "base_pass_at_1",
            "humaneval_plus_pass_at_1",
            "base_sample_pass_fraction",
            "humaneval_plus_sample_pass_fraction",
        ):
            if key in raw_summary:
                quality[key] = raw_summary[key]
    if scores_path is not None:
        rows = _read_jsonl(scores_path)
        quality["scored_samples"] = len(rows)
        quality["base_status_counts"] = dict(Counter(str(row.get("base_status", "missing")) for row in rows))
        quality["humaneval_plus_status_counts"] = dict(Counter(str(row.get("plus_status", "missing")) for row in rows))
    return quality or None


def _memory_summary(memory_paths: list[Path] | None, raw_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    per_call_rss = [_number(row.get("peak_rss_kib")) for row in raw_rows]
    observed_per_call_rss = [value for value in per_call_rss if value is not None]
    if not memory_paths:
        return {
            "sampler_files": [],
            "sampling_coverage_seconds": None,
            "sampling_rows": 0,
            "sampling_coverage_overlap_seconds": None,
            "peak_call_rss_kib": max(observed_per_call_rss) if observed_per_call_rss else None,
            "peak_resident_gtt_kib": None,
            "note": "No periodic process/GTT sampler file was supplied; per-call peak RSS is shown where available.",
        }

    rows = [row for memory_path in memory_paths for row in _read_jsonl(memory_path)]
    timestamps = [parsed for row in rows if (parsed := _date(row.get("timestamp"))) is not None]
    valid_rss = [value for row in rows if (value := _number(row.get("vmrss_kib"))) is not None]
    peak_rss = [value for row in rows if (value := _number(row.get("vmhwm_kib"))) is not None]
    gtt = [value for row in rows if (value := _number(row.get("resident_gtt_kib_deduplicated_sum"))) is not None]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    ordered_timestamps = sorted(set(timestamps))
    sample_intervals = [(right - left).total_seconds() for left, right in pairwise(ordered_timestamps)]
    raw_starts = [parsed for row in raw_rows if (parsed := _date(row.get("started_at_utc"))) is not None]
    raw_ends = [parsed for row in raw_rows if (parsed := _date(row.get("ended_at_utc"))) is not None]
    raw_start = min(raw_starts) if raw_starts else None
    raw_end = max(raw_ends) if raw_ends else None
    active_windows: list[tuple[datetime, datetime]] = []
    request_intervals = [
        (start, end)
        for row in raw_rows
        if (start := _date(row.get("started_at_utc"))) is not None
        and (end := _date(row.get("ended_at_utc"))) is not None
    ]
    if request_intervals:
        intervals = request_intervals
        intervals.sort()
        active_start, active_end = intervals[0]
        for request_start, request_end in intervals[1:]:
            if (request_start - active_end).total_seconds() > 300:
                active_windows.append((active_start, active_end))
                active_start = request_start
            active_end = max(active_end, request_end)
        active_windows.append((active_start, active_end))
    elif raw_start is not None and raw_end is not None:
        active_windows.append((raw_start, raw_end))
    active_window_seconds = sum(
        (window_end - window_start).total_seconds() for window_start, window_end in active_windows
    )
    overlap = None
    envelope_overlap = None
    raw_window = None
    sampling_segments: list[tuple[datetime, datetime]] = []
    if ordered_timestamps:
        max_gap_seconds = max(10.0, 5 * statistics.median(sample_intervals)) if sample_intervals else 10.0
        segment_start = ordered_timestamps[0]
        segment_end = ordered_timestamps[0]
        for timestamp in ordered_timestamps[1:]:
            if (timestamp - segment_end).total_seconds() > max_gap_seconds:
                sampling_segments.append((segment_start, segment_end))
                segment_start = timestamp
            segment_end = timestamp
        sampling_segments.append((segment_start, segment_end))
    if start is not None and end is not None and raw_start is not None and raw_end is not None:
        overlap = sum(
            max(0.0, (min(segment_end, raw_end) - max(segment_start, raw_start)).total_seconds())
            for segment_start, segment_end in sampling_segments
        )
        envelope_overlap = overlap
        overlap = sum(
            max(0.0, (min(sample_end, window_end) - max(sample_start, window_start)).total_seconds())
            for sample_start, sample_end in sampling_segments
            for window_start, window_end in active_windows
        )
        raw_window = max(0.0, (raw_end - raw_start).total_seconds())
    pid_values: set[int] = set()
    for row in rows:
        pid = row.get("pid")
        if isinstance(pid, int):
            pid_values.add(pid)
    pids = sorted(pid_values)
    return {
        "sampler_files": [str(memory_path) for memory_path in memory_paths],
        "sampling_rows": len(rows),
        "samples_with_timestamp": len(timestamps),
        "samples_with_rss": len(valid_rss),
        "samples_with_resident_gtt_counter": len(gtt),
        "pid_values": pids,
        "coverage_start_utc": start.isoformat() if start else None,
        "coverage_end_utc": end.isoformat() if end else None,
        "sampling_segment_count": len(sampling_segments),
        "sampling_coverage_segments": [
            {
                "start_utc": segment_start.isoformat(),
                "end_utc": segment_end.isoformat(),
                "duration_seconds": (segment_end - segment_start).total_seconds(),
            }
            for segment_start, segment_end in sampling_segments
        ],
        "sampling_coverage_seconds": sum(
            (segment_end - segment_start).total_seconds() for segment_start, segment_end in sampling_segments
        ),
        "median_sampling_interval_seconds": statistics.median(sample_intervals) if sample_intervals else None,
        "cohort_window_seconds": raw_window,
        "active_cohort_window_split_threshold_seconds": 300,
        "active_cohort_windows": [
            {
                "start_utc": window_start.isoformat(),
                "end_utc": window_end.isoformat(),
                "duration_seconds": (window_end - window_start).total_seconds(),
            }
            for window_start, window_end in active_windows
        ],
        "active_cohort_window_seconds": active_window_seconds,
        "sampling_coverage_overlap_seconds": overlap,
        "sampling_coverage_overlap_with_cohort_envelope_seconds": envelope_overlap,
        "sampling_coverage_fraction_of_cohort_window": (
            envelope_overlap / raw_window if envelope_overlap is not None and raw_window else None
        ),
        "sampling_coverage_fraction_of_active_cohort_window": (
            overlap / active_window_seconds if overlap is not None and active_window_seconds else None
        ),
        "peak_sampled_vmrss_kib": max(valid_rss) if valid_rss else None,
        "peak_process_vmhwm_kib": max(peak_rss) if peak_rss else None,
        "peak_call_rss_kib": max(observed_per_call_rss) if observed_per_call_rss else None,
        "peak_resident_gtt_kib_deduplicated": max(gtt) if gtt else None,
        "peak_resident_gtt_gib_binary": max(gtt) / (1024**2) if gtt else None,
    }


def summarize(
    raw_path: Path,
    output_path: Path,
    expected_samples: int,
    memory_paths: list[Path] | None,
    scores_path: Path | None,
    score_summary_path: Path | None,
    candidate: str | None,
) -> dict[str, Any]:
    rows = _read_jsonl(raw_path)
    repeat_zero = [row for row in rows if row.get("repeat") == 0]
    reasons = Counter(str(row.get("done_reason") or row.get("finish_reason") or "missing") for row in rows)
    first = _summarize_metrics(repeat_zero)
    full = _summarize_metrics(rows)
    result = {
        "candidate": candidate or raw_path.parent.name,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "raw_file": str(raw_path),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "samples_expected": expected_samples,
        "samples_received": len(rows),
        "samples_missing": max(0, expected_samples - len(rows)),
        "unique_task_ids": len({row.get("task_id") for row in rows}),
        "status_counts": dict(Counter(_status(row) for row in rows)),
        "finish_reason_counts": dict(reasons),
        "timing_method": {
            "wall": "Ollama wall_seconds or GenAI elapsed_seconds, as reported in the raw row.",
            "prefill": "Ollama prompt_eval_duration converted from nanoseconds; prefix-cache contribution is not separately reported. NPU prefill duration is unavailable.",
            "decode": "Ollama eval_duration converted from nanoseconds; NPU decode duration is derived as generated_tokens / reported throughput_tps.",
            "first_token": "TTFT is reported only when the NPU raw runner measured it; no client TTFT is inferred for non-streaming Ollama.",
        },
        "all_samples": full,
        "first_repeat_per_task": first,
        "first_repeat_note": "repeat=0 rows are task-major first samples; they reduce within-task prefix-cache reuse but are not isolated cold-start measurements. No prompt-token/prefill-duration throughput is inferred because Ollama does not expose uncached token counts.",
        "quality": _score_summary(scores_path, score_summary_path),
        "memory": _memory_summary(memory_paths, rows),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True, help="native raw-response JSONL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-samples", type=int, default=60)
    parser.add_argument("--memory", type=Path, action="append", help="periodic process/GTT sample JSONL (repeatable)")
    parser.add_argument("--scores", type=Path, help="per-sample EvalPlus score JSONL")
    parser.add_argument("--score-summary", type=Path, help="aggregate EvalPlus score summary JSON")
    parser.add_argument("--candidate", help="explicit candidate/backend label")
    args = parser.parse_args()
    if args.expected_samples < 1:
        parser.error("--expected-samples must be positive")
    result = summarize(
        args.raw, args.output, args.expected_samples, args.memory, args.scores, args.score_summary, args.candidate
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "samples_received": result["samples_received"],
                "samples_expected": result["samples_expected"],
                "status_counts": result["status_counts"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
