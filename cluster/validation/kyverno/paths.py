"""Runfiles lookups shared by the Kyverno policy tests."""

from __future__ import annotations

from pathlib import Path

from util.bazel.runfiles import get_required_path


def manifest(name: str) -> Path:
    """An input manifest from this package's testdata/.

    Deliberately not named `testdata`: pytest's default `python_functions = test*`
    collects any imported callable whose name starts with "test", so it would be
    picked up as a test and error out looking for a `name` fixture.
    """
    return get_required_path(f"_main/cluster/validation/kyverno/testdata/{name}")


def policy(file_name: str) -> Path:
    """A ClusterPolicy manifest, by file name, from cluster/k8s/kyverno/policies/."""
    return get_required_path(f"_main/cluster/k8s/kyverno/policies/{file_name}")
