"""Execute the exact residual-extraction recipe distributed in the skill zip."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest_bazel
from python.runfiles import runfiles

from devinfra.js.debundle.skills.shared.examples.isolated_edit import recorded_run


def test_shipped_recipe_reduces_residual_and_emits_the_expected_behavior(tmp_path: Path) -> None:
    files = runfiles.Create()
    assert files is not None
    engine = files.Rlocation(os.environ["DEBUNDLE_PEEL_ENGINE"])
    node = files.Rlocation(os.environ["DEBUNDLE_PEEL_NODE"])
    skill = files.Rlocation(os.environ["DEBUNDLE_PEEL_SKILL"])
    fixture = files.Rlocation("ducktape/devinfra/js/debundle/skills/shared/examples/fixture/spec_config.yaml")
    assert engine is not None
    assert node is not None
    assert skill is not None
    assert fixture is not None
    original = tmp_path / "inputs"
    shutil.copytree(Path(fixture).parent, original)
    (original / "modules/main/calculation.yaml").unlink()
    (original / "modules/main/values.yaml").write_text("members: [{selector: {binding: {name: initialValue}}}]\n")
    records = tmp_path / "records"
    records.mkdir()
    output = tmp_path / "output"
    recorded_run(
        [
            engine,
            "run",
            "--graphs",
            "--tree-config",
            str(original / "spec_config.yaml"),
            "--tree-modules",
            str(original / "modules"),
            "--tree-vendor-marks",
            str(original / "vendor_marks.yaml"),
            "--tree-source-root",
            str(original),
            "--out-root",
            str(output),
        ],
        tmp_path,
        records / "initial",
    )
    snapshot_text = recorded_run(
        [
            engine,
            "snapshot",
            "create",
            "--graph",
            str(output / "reports/tree/main/dependency_graph.json.gz"),
            "--destination",
            str(tmp_path / "worker"),
        ],
        tmp_path,
        records / "snapshot",
    )
    snapshot_report = tmp_path / "snapshot_report.json"
    snapshot_report.write_text(snapshot_text)
    extracted = tmp_path / "skill"
    with zipfile.ZipFile(skill) as archive:
        archive.extractall(extracted)
    recipe = extracted / "debundle_lane_worker/references/skills/shared/examples/peel_more.py"
    assert recipe.is_file()
    result = subprocess.run(
        [
            sys.executable,
            str(recipe),
            "--snapshot-report",
            str(snapshot_report),
            "--module-subdir",
            "main",
            "--evidence",
            str(records / "peel"),
            "--max-checks",
            "1000",
        ],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(recipe.parent)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    report = json.loads(result.stdout)
    assert report["status"] == "found"
    assert report["residual_units_extracted"] == 3
    assert report["existing_units_moved"] == 0
    for step in ("baseline", "propose", "check", "preview", "apply", "regenerate"):
        assert json.loads((records / "peel" / f"{step}.result.json").read_text()) == {"exit_code": 0}
    snapshot = json.loads(snapshot_text)
    regeneration = snapshot["regeneration_argv"]
    regenerated = Path(regeneration[regeneration.index("--out-root") + 1])
    behavior = subprocess.run(
        [node, str(regenerated / "app/main/entry.js")], capture_output=True, text=True, check=False
    )
    assert behavior.returncode == 0, behavior.stderr
    assert behavior.stdout == "7\n"


if __name__ == "__main__":
    pytest_bazel.main()
