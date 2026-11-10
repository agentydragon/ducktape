"""Exercise the central proxy's alert expressions with upstream promtool."""

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one
from python.runfiles import Runfiles

from cluster.cdk8s.x.github_api_proxy import proxy


def test_github_proxy_rules(runfiles: Runfiles, promtool: Path, scratch_app: App, tmp_path: Path) -> None:
    manifest = one(
        manifest
        for manifest in Cdk8sTesting.synth(proxy.app_chart(scratch_app))
        if manifest["kind"] == "PrometheusRule"
    )
    (tmp_path / "github-proxy.yaml").write_text(yaml.safe_dump(manifest["spec"]))
    runfiles_root = runfiles.root()
    (tmp_path / "tests.yaml").write_text(
        (runfiles_root / "ducktape/cluster/cdk8s/x/github_api_proxy/testdata/github_proxy_rules.yaml").read_text()
    )
    subprocess.run([promtool, "test", "rules", "tests.yaml"], cwd=tmp_path, check=True)


if __name__ == "__main__":
    pytest_bazel.main()
