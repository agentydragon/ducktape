"""Exercise the central proxy's deployed alert expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def test_github_proxy_rules(tmp_path: Path) -> None:
    manifest = one(
        manifest
        for manifest in yaml.safe_load_all(
            get_required_path("_main/cluster/k8s/github-api-proxy/app/github-api-proxy.k8s.yaml").read_text()
        )
        if manifest["kind"] == "PrometheusRule"
    )
    (tmp_path / "github-proxy.yaml").write_text(yaml.safe_dump(manifest["spec"]))
    (tmp_path / "tests.yaml").write_text(
        get_required_path("_main/cluster/validation/testdata/github_proxy_rules.yaml").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
