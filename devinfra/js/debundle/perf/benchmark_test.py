"""Focused tests for benchmark bounds and failure reporting."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest_bazel

from devinfra.js.debundle.perf import benchmark


def test_time_record_parses_wall_cpu_memory_and_exit_status() -> None:
    record = benchmark.parse_time_json(
        '{"elapsed_seconds":1.25,"user_seconds":0.75,"system_seconds":0.25,"max_rss_kib":1234,"exit_status":7}'
    )

    assert record.elapsed_seconds == 1.25
    assert record.user_seconds == 0.75
    assert record.system_seconds == 0.25
    assert record.max_rss_kib == 1234
    assert record.exit_status == 7


def test_gnu_time_runs_inside_limits_and_measures_the_native_child(tmp_path: Path) -> None:
    command = benchmark.limited_command(
        ["/bin/true"], time_binary="/usr/bin/time", time_output=tmp_path / "time.json", timeout_seconds=10
    )

    assert command[0] == "timeout"
    assert command[command.index("--") + 1 : command.index("--") + 4] == ["/usr/bin/time", "--quiet", "--format"]
    assert command[-1] == "/bin/true"


def test_bounded_command_times_out_real_child() -> None:
    completed = subprocess.run(
        benchmark.bounded_command([sys.executable, "-c", "import time; time.sleep(30)"], 1), check=False
    )

    assert completed.returncode == 124
    assert benchmark.classify_status(completed.returncode, {}, {}) == "timed_out"


def test_bounded_command_preserves_child_failure_status() -> None:
    completed = subprocess.run(
        benchmark.bounded_command([sys.executable, "-c", "raise SystemExit(23)"], 30), check=False
    )

    assert completed.returncode == 23
    assert benchmark.classify_status(completed.returncode, {}, {}) == "child_failed"


def test_kill_is_oom_only_with_observed_cgroup_counter_delta() -> None:
    before: dict[str, int | None] = {"oom_kill_count": 4}
    assert benchmark.classify_status(137, before, {"oom_kill_count": 4}) == "sigkill_without_cgroup_oom_evidence"
    assert benchmark.classify_status(137, before, {"oom_kill_count": 5}) == "cgroup_oom_kill"


def test_failure_classification_separates_allocator_and_signal_statuses() -> None:
    assert benchmark.classify_status(1, {}, {}, "memory allocation of 1048576 bytes failed") == "allocation_failure"
    assert benchmark.classify_status(143, {}, {}) == "signal_or_signal_encoded_exit_status"


def test_empty_emission_is_not_a_valid_success() -> None:
    accepted, evidence = benchmark.valid_summary(
        {"chunks": {"main": {"gate_accepts": True, "translation_validation_accepts": True}}, "emitted": {}}
    )

    assert not accepted
    assert evidence["emitted_file_count"] == 0


def test_early_failure_writes_machine_readable_artifact(tmp_path: Path) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)

    benchmark.persist_failure(tmp_path, started_at, RuntimeError("tool missing"))

    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["complete"] is False
    assert metrics["failure"] == {"type": "RuntimeError", "message": "tool missing"}
    assert metrics["started_at_utc"] == started_at.isoformat()
    assert "tool missing" in (tmp_path / "summary.md").read_text()


if __name__ == "__main__":
    pytest_bazel.main()
