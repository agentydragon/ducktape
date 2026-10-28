"""Execute the packed recipe from its shipped relative layout."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest_bazel
from python.runfiles import runfiles


def test_callback_contract_recipe_and_conservative_controls(tmp_path: Path) -> None:
    files = runfiles.Create()
    assert files is not None
    debundle = files.Rlocation(os.environ["DEBUNDLE_CONTRACT_ENGINE"])
    node = files.Rlocation(os.environ["DEBUNDLE_CONTRACT_NODE"])
    skill = files.Rlocation(os.environ["DEBUNDLE_CONTRACT_SKILL"])
    assert debundle is not None
    assert node is not None
    assert skill is not None

    extracted = tmp_path / "skill"
    with zipfile.ZipFile(skill) as archive:
        archive.extractall(extracted)
    shared = extracted / "debundle_lane_worker/references/skills/shared"
    script = shared / "examples/contract_examples.py"
    assert script.is_file()

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--debundle",
            debundle,
            "--node",
            node,
            "--fixtures",
            "examples/contract_examples",
            "--out",
            "out",
        ],
        cwd=shared,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    for name in ("fresh_factory", "retained_constructor", "persistent_registry"):
        assert (shared / "out" / name / "app/main/entry.js").is_file()
    expected_conflicts = {
        "fresh_factory": {"callbacks/wrapped", "values/later"},
        "retained_constructor": {"callbacks/wrapped", "values/later"},
        "persistent_registry": {"callbacks/registration", "values/later"},
    }
    for name, modules in expected_conflicts.items():
        report = shared / "out/controls" / name / "reports/tree/main/atomic_group_conflicts.json"
        conflicts = json.loads(report.read_text())
        assert any(modules <= {claim["module"] for claim in conflict["claims"]} for conflict in conflicts)


if __name__ == "__main__":
    pytest_bazel.main()
