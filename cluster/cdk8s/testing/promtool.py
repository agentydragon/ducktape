"""Run a selected Prometheus rule test with the upstream promtool binary."""

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from python.runfiles import Runfiles

PromtoolRules = Callable[[str, Mapping[str, dict[str, Any]]], None]


@pytest.fixture
def promtool(runfiles: Runfiles, tmp_path: Path) -> PromtoolRules:
    promtool_path = runfiles.root() / os.environ["PROMTOOL_RLOCATION"]

    def run(testdata_path: str, rule_files: Mapping[str, dict[str, Any]]) -> None:
        for filename, spec in rule_files.items():
            (tmp_path / filename).write_text(yaml.safe_dump(spec))
        (tmp_path / "tests.yaml").write_text((runfiles.root() / testdata_path).read_text())
        subprocess.run([promtool_path, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)

    return run
