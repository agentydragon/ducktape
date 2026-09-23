"""Exercise the deployed PrometheusRule expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path


def test_github_quota_rules(tmp_path: Path) -> None:
    manifests = {
        manifest["metadata"]["name"]: manifest
        for manifest in yaml.safe_load_all(
            get_required_path("_main/cluster/generated/monitoring/rules/monitoring-rules.k8s.yaml").read_text()
        )
    }
    for name in ("github-quota", "roaming-node"):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(manifests[f"{name}-alerts"]["spec"]))
    (tmp_path / "tests.yaml").write_text(
        get_required_path("_main/cluster/validation/testdata/github_quota_rules.yaml").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
