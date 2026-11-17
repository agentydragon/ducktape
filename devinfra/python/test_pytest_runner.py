"""Exercise collection and failure status through the real pytest entrypoint."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from devinfra.testing.output_writer import BoundTestOutputs


def test_runner_child(test_outputs: BoundTestOutputs) -> None:
    # A Settings-like parser must not receive the collection path.
    parser = argparse.ArgumentParser()
    parser.add_argument("-p")
    parser.parse_args()
    assert test_outputs.case_id.endswith("test_pytest_runner.py::test_runner_child")
    if os.environ.get("DEFAULT_RUNNER_FAILURE_PROBE"):
        pytest.fail("intentional runner failure probe")


@pytest.mark.parametrize(
    ("test_filter", "shard", "exit_code", "case_count"),
    [
        ("test_runner_child", 0, 0, 1),
        ("test_runner_child", 1, 0, 0),
        ("test_runner_child", 0, 1, 1),
        ("no_such_probe_case", 0, 4, None),
    ],
)
def test_built_runner_collects_filters_shards_and_propagates_failure(
    tmp_path: Path, test_filter: str, shard: int, exit_code: int, case_count: int | None
) -> None:
    (tmp_path / "child").mkdir()
    environment = os.environ | {
        "TEST_TMPDIR": str(tmp_path / "child"),
        "TESTBRIDGE_TEST_ONLY": f"{test_filter} and not test_built_runner",
        "TEST_TOTAL_SHARDS": "2",
        "TEST_SHARD_INDEX": str(shard),
        "TEST_SHARD_STATUS_FILE": str(tmp_path / "shard-status"),
        "XML_OUTPUT_FILE": str(tmp_path / "results.xml"),
    }
    if exit_code == 1:
        environment["DEFAULT_RUNNER_FAILURE_PROBE"] = "1"
    # Preserve this real Bazel target's collection environment and plugin arguments.
    # TESTBRIDGE_TEST_ONLY selects the one child case, preventing recursive execution.
    result = subprocess.run(
        [sys.executable, "-m", "pytest_bazel", *sys.argv[1:]],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    if case_count is not None:
        report = ElementTree.parse(tmp_path / "results.xml")
        assert len(report.findall(".//testcase")) == case_count
        assert len(report.findall(".//failure")) == (1 if exit_code == 1 else 0)
    assert (tmp_path / "shard-status").exists()
