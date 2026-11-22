"""Render the Flux bootstrap tree using the Flux CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

from cluster.validation.flux import parse_flux_kustomizations
from cluster.validation.k8s import load_k8s_resources


def test_flux_build_renders_tree(repo_root: Path, runfiles: Runfiles) -> None:
    sync = repo_root / "cluster/k8s/flux/flux-system/gotk-sync.yaml"
    root = parse_flux_kustomizations(sync)["flux-system"]
    result = subprocess.run(
        [
            runfiles.root() / "multitool/tools/flux/flux",
            "build",
            "kustomization",
            "flux-system",
            "--namespace",
            root.namespace,
            "--path",
            repo_root / root.path,
            "--kustomization-file",
            sync,
            "--dry-run",
        ],
        stdout=subprocess.PIPE,
        check=True,
        timeout=60,
    )
    resources = load_k8s_resources(result.stdout, kinds={"Kustomization", "GitRepository"})
    assert root.source_ref is not None
    source = root.source_ref
    assert (source.kind, source.namespace or root.namespace, source.name) in {
        (resource.kind, resource.namespace, resource.name) for resource in resources
    }
    assert any(resource.kind == "Kustomization" and resource.namespace == "ducktape-flux" for resource in resources)


if __name__ == "__main__":
    pytest_bazel.main()
