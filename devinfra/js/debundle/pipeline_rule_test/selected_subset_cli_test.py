"""The wrapped graph CLI relocates and verifies its ownership-subset runfile."""

import json
import os
import subprocess
from pathlib import Path

from python.runfiles import Runfiles


def test_selected_graph_cli_uses_relocated_subset_runfile(runfiles: Runfiles) -> None:
    files = runfiles.root()
    runner = str(files / os.environ["RUNNER"])
    root = (Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]).resolve()
    result = subprocess.run(
        [
            runner,
            "--debundler",
            os.environ["DEBUNDLER"],
            "--graph",
            os.environ["GRAPH_TREE"],
            "--graph-relpath",
            "reports/tree/main/dependency_graph.json.gz",
            "--modules-path",
            "devinfra/js/debundle/pipeline_rule_test/fixture/modules",
            "--source-root",
            "runfile",
            os.environ["SOURCE_TREE"],
            "--spec-root",
            "devinfra/js/debundle/pipeline_rule_test/fixture",
            "--ownership-subset-root",
            os.environ["OWNERSHIP_SUBSET"],
            "--graph-source-root",
            os.environ["GRAPH_SOURCE_TREE"],
            "--",
            "modules",
            "propose",
            "--tree-config",
            str(root / "devinfra/js/debundle/pipeline_rule_test/fixture/config.yaml"),
            "--into",
            "candidate",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=root,
        env={**os.environ, "BUILD_WORKSPACE_DIRECTORY": str(root)},
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\n{result.stdout}"
    report = json.loads(result.stdout)
    # The selected `values` owner claims the fixture's only source unit. A
    # relocated selected graph can therefore prove this residual is exhausted.
    assert report["status"] == "exhausted_no_peel"
    assert report["plan"] is None
