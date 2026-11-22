"""Render the Cilium chart with the bootstrap values."""

import subprocess

import pytest_bazel
from python.runfiles import Runfiles


def test_helm_templates_render(runfiles: Runfiles) -> None:
    root = runfiles.root()
    subprocess.run(
        [
            root / "multitool/tools/helm/helm",
            "template",
            "test-release",
            "cilium",
            "--repo",
            "https://helm.cilium.io/",
            "--values",
            root / "ducktape/cluster/terraform/main/cilium-values.yaml",
            "--dry-run=client",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    pytest_bazel.main()
