"""Runfiles lookups shared by the Kyverno policy tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

_POLICIES = "_main/cluster/generated/kyverno/policies/policies.k8s.yaml"


def manifest(name: str) -> Path:
    """An input manifest from this package's testdata/.

    Deliberately not named `testdata`: pytest's default `python_functions = test*`
    collects any imported callable whose name starts with "test", so it would be
    picked up as a test and error out looking for a `name` fixture.
    """
    return get_required_path(f"_main/cluster/validation/kyverno/testdata/{name}")


def policy(name: str, directory: Path) -> Path:
    """The ClusterPolicy `name` from cluster/generated/kyverno/policies/, alone in a file under
    `directory`: `kyverno apply` runs every policy a file holds, and that directory's policies
    share one."""
    path = directory / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(
            one(
                doc
                for doc in yaml.safe_load_all(get_required_path(_POLICIES).read_text())
                if doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == name
            )
        )
    )
    return path
