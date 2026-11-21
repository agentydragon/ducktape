"""Render the Flux bootstrap tree using the Flux CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.validation.k8s import parse_k8s_resources


def test_flux_build_renders_tree(k8s_dir: Path) -> None:
    flux_bin = Runfiles.CreateOrRaise().root() / "multitool/tools/flux/flux"
    result = subprocess.run(
        [
            flux_bin,
            "build",
            "kustomization",
            "flux-system",
            "--path",
            k8s_dir,
            "--kustomization-file",
            k8s_dir / "flux/flux-system/gotk-sync.yaml",
            "--dry-run",
            "--verbose",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"flux build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    kinds = {r.kind for r in parse_k8s_resources(yaml.safe_load_all(result.stdout))}
    missing = {"Kustomization", "GitRepository"} - kinds
    assert not missing, f"flux build did not render {sorted(missing)}:\n{result.stderr}"


if __name__ == "__main__":
    pytest_bazel.main()
