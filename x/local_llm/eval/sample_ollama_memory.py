"""Sample Ollama process RSS and deduplicated Intel DRM GTT usage."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) else 0


def _status_kib(pid: int, key: str) -> int | None:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        name, separator, value = line.partition(":")
        if separator and name == key:
            fields = value.split()
            if fields:
                return int(fields[0])
    return None


def sample(pid: int) -> dict[str, object]:
    clients: dict[int, dict[str, object]] = {}
    for fdinfo in Path(f"/proc/{pid}/fdinfo").iterdir():
        fields = {}
        for line in fdinfo.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"drm-client-id", "drm-resident-gtt", "drm-total-gtt"}:
                fields[key] = value.strip()
        if "drm-client-id" not in fields:
            continue
        client_id = int(fields["drm-client-id"])
        client = clients.setdefault(
            client_id, {"drm_client_id": client_id, "fd_count": 0, "resident_gtt_kib": [], "total_gtt_kib": []}
        )
        fd_count = client["fd_count"]
        assert isinstance(fd_count, int)
        client["fd_count"] = fd_count + 1
        for field, output in (("drm-resident-gtt", "resident_gtt_kib"), ("drm-total-gtt", "total_gtt_kib")):
            if field in fields:
                values = client[output]
                assert isinstance(values, list)
                values.append(int(fields[field].split()[0]))
    unique_clients = []
    for client in clients.values():
        for key in ("resident_gtt_kib", "total_gtt_kib"):
            values = client[key]
            assert isinstance(values, list)
            client[key] = max(values) if values else None
            client[f"{key}_fd_values_agree"] = len(set(values)) <= 1
        unique_clients.append(client)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "pid": pid,
        "vmrss_kib": _status_kib(pid, "VmRSS"),
        "vmhwm_kib": _status_kib(pid, "VmHWM"),
        "drm_clients": unique_clients,
        "resident_gtt_kib_deduplicated_sum": sum(_int_or_zero(client["resident_gtt_kib"]) for client in unique_clients),
        "total_gtt_kib_deduplicated_sum": sum(_int_or_zero(client["total_gtt_kib"]) for client in unique_clients),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int, help="host PID of the selected Ollama server")
    parser.add_argument("output", type=Path, help="new JSONL file for periodic samples")
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--interval-seconds", type=float, default=1)
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("duration and interval must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with args.output.open("x", encoding="utf-8") as output:
        while True:
            try:
                record = sample(args.pid)
            except (FileNotFoundError, PermissionError, ProcessLookupError) as error:
                record = {"timestamp": datetime.now(UTC).isoformat(), "pid": args.pid, "error": str(error)}
            output.write(json.dumps(record, sort_keys=True) + "\n")
            output.flush()
            elapsed = time.monotonic() - started
            if elapsed >= args.duration_seconds:
                break
            time.sleep(min(args.interval_seconds, args.duration_seconds - elapsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
