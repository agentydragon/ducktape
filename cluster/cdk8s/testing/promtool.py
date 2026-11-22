"""Run a selected Prometheus rule test with the upstream promtool binary."""

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from python.runfiles import Runfiles

type PromtoolTestData = str | Mapping[str, Any]
PromtoolRules = Callable[[PromtoolTestData, Mapping[str, dict[str, Any]]], None]


@pytest.fixture
def promtool(runfiles: Runfiles, tmp_path: Path) -> PromtoolRules:
    promtool_path = runfiles.root() / os.environ["PROMTOOL_RLOCATION"]

    def run(testdata: PromtoolTestData, rule_files: Mapping[str, dict[str, Any]]) -> None:
        for filename, spec in rule_files.items():
            (tmp_path / filename).write_text(yaml.safe_dump(spec))
        tests_yaml = (
            (runfiles.root() / testdata).read_text() if isinstance(testdata, str) else yaml.safe_dump(dict(testdata))
        )
        (tmp_path / "tests.yaml").write_text(tests_yaml)
        subprocess.run([promtool_path, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)

    return run
