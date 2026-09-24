"""Exercise the central proxy's alert expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.github_api_proxy import proxy
from util.bazel.runfiles import get_required_path


def test_github_proxy_rules(tmp_path: Path) -> None:
    manifest = one(
        manifest
        for manifest in Cdk8sTesting.synth(proxy.app_chart(Cdk8sTesting.app()))
        if manifest["kind"] == "PrometheusRule"
    )
    (tmp_path / "github-proxy.yaml").write_text(yaml.safe_dump(manifest["spec"]))
    (tmp_path / "tests.yaml").write_text(
        get_required_path("_main/cluster/cdk8s/github_api_proxy/testdata/github_proxy_rules.yaml").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
