"""Tests for Flux Kustomization path resolution."""

from pathlib import Path

import pytest_bazel

from cluster.validation.flux import FluxKustomizationSpec


def test_both_manifest_roots_are_local(tmp_path: Path) -> None:
    for root in ("cluster/k8s", "cluster/generated"):
        assert FluxKustomizationSpec(path=f"./{root}/app").local_dir(tmp_path) == (tmp_path / root / "app").resolve()
    # A path in another source (an upstream GitRepository, a project's own deploy/) is not.
    assert FluxKustomizationSpec(path="./deploy").local_dir(tmp_path) is None


if __name__ == "__main__":
    pytest_bazel.main()
