"""Execute the packed recipe from its shipped relative layout."""

import json
import subprocess
import sys
from pathlib import Path

import pytest_bazel


def test_callback_contract_recipe_and_conservative_controls(shared: Path, engine: str, node: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(shared / "examples/contract_examples.py"),
            "--debundle",
            engine,
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

    expected_conflicts = {
        "fresh_factory": {"callbacks/wrapped", "values/later"},
        "retained_constructor": {"callbacks/wrapped", "values/later"},
        "persistent_registry": {"callbacks/registration", "values/later"},
    }
    for name, modules in expected_conflicts.items():
        assert (shared / "out" / name / "app/main/entry.js").is_file()
        report = shared / "out/controls" / name / "reports/tree/main/atomic_group_conflicts.json"
        conflicts = json.loads(report.read_text())
        assert any(modules <= {claim["module"] for claim in conflict["claims"]} for conflict in conflicts)


if __name__ == "__main__":
    pytest_bazel.main()
