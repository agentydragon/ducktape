"""Execute the exact residual-extraction recipe distributed in the skill zip."""

import gzip
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml
from python.runfiles import runfiles

from devinfra.js.debundle.skills.shared.examples.isolated_edit import recorded_run


def entry_source_units(graph_path: Path) -> set[str]:
    with gzip.open(graph_path, "rt", encoding="utf-8") as graph_file:
        graph = json.load(graph_file)
    entry_keys = {module["key"] for module in graph["module_graph"]["nodes"] if module["kind"] == "entry"}
    return {node["id"] for node in graph["nodes"] if node["destination"] in entry_keys}


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
    config_path = original / "spec_config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["import_order"] = {"main": ["values"]}
    config_path.write_text(yaml.safe_dump(config))
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
    baseline_entry_units = entry_source_units(output / "reports/tree/main/dependency_graph.json.gz")
    snapshot_report = tmp_path / "snapshot_report.json"
    snapshot_report.write_text(snapshot_text)
    snapshot = json.loads(snapshot_text)
    authored_owner = Path(snapshot["modules"]) / "main/values.yaml"
    authored_owner_before = authored_owner.read_bytes()
    extracted = tmp_path / "skill"
    with zipfile.ZipFile(skill) as archive:
        archive.extractall(extracted)
    recipe = extracted / "debundle_lane_worker/references/skills/shared/examples/peel_more.py"
    assert recipe.is_file()
    result = subprocess.run(
        [sys.executable, str(recipe), "--snapshot-report", str(snapshot_report), "--evidence", str(records / "peel")],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(recipe.parent)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    report = json.loads(result.stdout)
    assert report["status"] == "found"
    selected_units = set(report["selected_source_units"])
    planned_units = {change["unit_id"] for change in report["plan"]["changes"]}
    assert selected_units == planned_units
    assert report["residual_units_extracted"] == len(selected_units) > 0
    for step in ("baseline", "propose", "check", "preview", "apply", "regenerate"):
        assert json.loads((records / "peel" / f"{step}.result.json").read_text()) == {"exit_code": 0}
    checked = json.loads((records / "peel/check.stdout").read_text())
    assert checked["accepted"] is True
    assert checked["plan"] == report["plan"]
    check_invocation = json.loads((records / "peel/check.invocation.json").read_text())
    apply_invocation = json.loads((records / "peel/apply.invocation.json").read_text())
    assert check_invocation["argv"][check_invocation["argv"].index("--plan") + 1] == str(records / "peel/plan.json")
    assert apply_invocation["argv"][apply_invocation["argv"].index("--plan") + 1] == str(records / "peel/plan.json")
    applied = json.loads((records / "peel/apply.stdout").read_text())
    assert applied["residual_source_units_claimed"] == len(selected_units)
    assert applied["source_units_reassigned"] == 0
    assert authored_owner.read_bytes() == authored_owner_before
    regeneration = snapshot["regeneration_argv"]
    regenerated = Path(regeneration[regeneration.index("--out-root") + 1])
    emitted_entry_units = entry_source_units(regenerated / "reports/tree/main/dependency_graph.json.gz")
    assert baseline_entry_units - emitted_entry_units == selected_units
    behavior = subprocess.run(
        [node, str(regenerated / "app/main/entry.js")], capture_output=True, text=True, check=False
    )
    assert behavior.returncode == 0, behavior.stderr
    assert behavior.stdout == "7\n"
