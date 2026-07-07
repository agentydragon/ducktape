#!/usr/bin/env python3
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))]


def aggregate(values: list[float]) -> dict[str, float]:
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def workload_metric(workload: dict[str, Any]) -> dict[str, float]:
    name = workload["name"]
    if name.startswith(("autocommit_", "activitywatch_batch_")):
        return {key: float(workload[key]) for key in ("p50_ms", "p95_ms", "max_ms", "mean_ms")}
    if name == "grocy_batch_100":
        return {
            "write_p50_ms": float(workload["write"]["p50_ms"]),
            "write_p95_ms": float(workload["write"]["p95_ms"]),
            "write_max_ms": float(workload["write"]["max_ms"]),
            "query_p50_ms": float(workload["indexed_product_query"]["p50_ms"]),
            "query_p95_ms": float(workload["indexed_product_query"]["p95_ms"]),
            "query_max_ms": float(workload["indexed_product_query"]["max_ms"]),
        }
    if name.startswith("queries_"):
        return {
            "time_range_p50_ms": float(workload["time_range"]["p50_ms"]),
            "time_range_p95_ms": float(workload["time_range"]["p95_ms"]),
            "time_range_max_ms": float(workload["time_range"]["max_ms"]),
            "bucket_count_p50_ms": float(workload["bucket_count"]["p50_ms"]),
            "bucket_count_p95_ms": float(workload["bucket_count"]["p95_ms"]),
            "bucket_count_max_ms": float(workload["bucket_count"]["max_ms"]),
            "close_reopen_count_ms": float(workload["close_reopen_count_ms"]),
        }
    if name == "wal_checkpoint_truncate":
        return {key: float(workload[key]) for key in ("checkpoint_ms", "close_ms", "reopen_count_ms")}
    raise ValueError(f"unknown workload {name}")


def read_logs(result_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metadata = []
    results = []
    for log_path in sorted((result_dir / "logs").glob("*.jsonl")):
        run_metadata: dict[str, Any] | None = None
        for line in log_path.read_text().splitlines():
            record = json.loads(line)
            if record["record"] == "metadata":
                run_metadata = record
                metadata.append(record)
            elif record["record"] == "result":
                if run_metadata is None:
                    raise ValueError(f"{log_path} emitted result before metadata")
                results.append(
                    {
                        "storage_class": run_metadata["storage_class"],
                        "repeat": run_metadata["repeat"],
                        "node_name": run_metadata["node_name"],
                        "workload": record["workload"],
                    }
                )
    return metadata, results


def write_csv(result_dir: Path, rows: list[dict[str, Any]]) -> None:
    csv_path = result_dir / "summary.csv"
    fieldnames = sorted({key for row in rows for key in row})
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    args = parser.parse_args()
    metadata, results = read_logs(args.result_dir)
    rows: list[dict[str, Any]] = []
    for result in results:
        workload = result["workload"]
        rows.append(
            {
                "storage_class": result["storage_class"],
                "repeat": result["repeat"],
                "node_name": result["node_name"],
                "workload": workload["name"],
                **workload_metric(workload),
                **({"inserts_per_sec": workload["inserts_per_sec"]} if "inserts_per_sec" in workload else {}),
            }
        )
    write_csv(args.result_dir, rows)

    print(f"# SQLite storage benchmark summary ({args.result_dir.name})")
    print()
    print(f"- Runs parsed: {len(metadata)}")
    print(f"- Result rows: {len(rows)}")
    print()
    print("## Nodes")
    print()
    for item in metadata:
        print(f"- {item['storage_class']} repeat {item['repeat']}: {item['node_name']}")
    print()
    print("## Aggregated metrics")
    print()
    print("| StorageClass | Workload | Metric | p50 | p95 | max |")
    print("| --- | --- | --- | ---: | ---: | ---: |")

    by_metric: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        for key, value in row.items():
            if key in {"storage_class", "repeat", "node_name", "workload"}:
                continue
            by_metric[(row["storage_class"], row["workload"], key)].append(float(value))

    for (storage_class, workload, metric), values in sorted(by_metric.items()):
        stats = aggregate(values)
        print(
            f"| `{storage_class}` | `{workload}` | `{metric}` | {stats['p50']:.3f} | {stats['p95']:.3f} | {stats['max']:.3f} |"
        )


if __name__ == "__main__":
    main()
