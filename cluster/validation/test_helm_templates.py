"""Render the Cilium chart with the bootstrap values."""

import subprocess

import pytest_bazel
from python.runfiles import Runfiles


def test_helm_templates_render(runfiles: Runfiles) -> None:
    root = runfiles.root()
    bootstrap = root / "ducktape/cluster/terraform/main"
    subprocess.run(
        [
            root / "multitool/tools/helm/helm",
            "template",
            "cilium",
            "cilium",
            "--repo",
            "https://helm.cilium.io/",
            "--version",
            (bootstrap / "cilium-version.txt").read_text().strip(),
            "--namespace",
            "kube-system",
            "--values",
            bootstrap / "cilium-values.yaml",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    pytest_bazel.main()
