"""Public acceptance round for the exact recipe shipped with the agent skills."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from python.runfiles import runfiles

from devinfra.js.debundle.skills.shared.examples.isolated_edit import (
    Snapshot,
    graph_command,
    merge_and_regenerate,
    recorded_run,
)
from util.testing.undeclared_outputs import undeclared_outputs_dir


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{argv}\n{result.stdout}\n{result.stderr}"
    return result


def test_worker_patch_is_integrated_only_after_baseline_verification(tmp_path: Path) -> None:
    files = runfiles.Create()
    assert files is not None
    engine = files.Rlocation(os.environ["DEBUNDLE_HANDOFF_ENGINE"])
    node = files.Rlocation(os.environ["DEBUNDLE_HANDOFF_NODE"])
    fixture_config = files.Rlocation("_main/devinfra/js/debundle/skills/shared/examples/fixture/spec_config.yaml")
    assert engine is not None
    assert node is not None
    assert fixture_config is not None
    original = tmp_path / "original inputs"
    shutil.copytree(Path(fixture_config).parent, original)
    original_output = run([node, str(original / "source/main.js")], tmp_path).stdout
    output = tmp_path / "baseline output"
    packages = tmp_path / "package inputs"
    packages.mkdir()
    run(
        [
            engine,
            "run",
            "--tree-config",
            str(original / "spec_config.yaml"),
            "--tree-modules",
            str(original / "modules"),
            "--tree-vendor-marks",
            str(original / "vendor_marks.yaml"),
            "--tree-source-root",
            str(original),
            "--packages-root",
            str(packages),
            "--out-root",
            str(output),
        ],
        tmp_path,
    )
    graph = output / "reports/tree/main/dependency_graph.json"

    def snapshot(name: str) -> Snapshot:
        return cast(
            Snapshot,
            json.loads(
                run(
                    [engine, "snapshot", "create", "--graph", str(graph), "--destination", str(tmp_path / name)],
                    tmp_path,
                ).stdout
            ),
        )

    worker = snapshot("worker copy")
    integration = snapshot("integration copy")
    assert "__packages_root__" in worker["graph_package_roots"]
    # Both copies must remain usable without their original spec, source or graph.
    shutil.rmtree(original)
    shutil.rmtree(output)
    shutil.rmtree(packages)
    evidence = undeclared_outputs_dir() / "delegation"
    evidence.mkdir()
    worker_records = evidence / "worker"
    merge_and_regenerate(worker, Path("main"), ["values", "calculation"], "arithmetic", worker_records)
    assert json.loads((worker_records / "apply.stdout").read_text())["gate"] == "passed"
    for step in ("baseline", "preview", "apply", "regenerate"):
        assert json.loads((worker_records / f"{step}.result.json").read_text()) == {"exit_code": 0}
    plan = json.loads((worker_records / "apply.plan.json").read_text())
    assert plan["executable"] == worker["engine"]
    assert plan["argv"] == json.loads((worker_records / "apply.invocation.json").read_text())["argv"]

    # A recorded invocation can exist for a refused operation. Keep the actual
    # nonzero result, and do not mistake it for post-edit validation success.
    with pytest.raises(RuntimeError, match="command exited 1"):
        recorded_run(
            [
                *graph_command(worker),
                "describe",
                "addIncrement",
                "--graph",
                worker["graph"],
                "--modules",
                str(Path(worker["modules"]) / "main"),
                "--source-root",
                worker["source_root"],
                "--format",
                "json",
            ],
            Path(worker["directory"]),
            evidence / "stale-query",
        )
    assert json.loads((evidence / "stale-query.result.json").read_text()) == {"exit_code": 1}
    assert (evidence / "stale-query.invocation.json").is_file()
    refusal = json.loads((evidence / "stale-query.stdout").read_text())
    assert refusal["kind"] == "graph_compatibility"
    assert refusal["mismatch"]["reason"] == "yaml_membership"

    # Verify the receiving baseline BEFORE applying the reviewed YAML delta.
    recorded_run(
        [integration["engine"], "snapshot", "verify", integration["directory"]],
        Path(integration["directory"]),
        evidence / "integration-baseline",
    )
    worker_manifest = Path(worker["directory"]) / "snapshot.json"
    integration_manifest = Path(integration["directory"]) / "snapshot.json"
    assert (
        json.loads(worker_manifest.read_text())["graph_sha256"]
        == json.loads(integration_manifest.read_text())["graph_sha256"]
    )
    for name in ("values.yaml", "calculation.yaml"):
        assert not (Path(worker["modules"]) / "main" / name).exists()
        (Path(integration["modules"]) / "main" / name).unlink()
    shutil.copyfile(
        Path(worker["modules"]) / "main/arithmetic.yaml", Path(integration["modules"]) / "main/arithmetic.yaml"
    )
    recorded_run(integration["regeneration_argv"], Path(integration["directory"]), evidence / "integration")
    for snapshot_info in (worker, integration):
        entry = Path(snapshot_info["directory"]) / "output/app/main/entry.js"
        actual = run([node, str(entry)], tmp_path)
        assert actual.stdout == original_output == "7\n"
    shutil.copyfile(worker_manifest, evidence / "worker-baseline.json")
    shutil.copyfile(integration_manifest, evidence / "integration-baseline.json")


if __name__ == "__main__":
    pytest_bazel.main()
