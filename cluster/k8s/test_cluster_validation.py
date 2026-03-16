"""Integration tests: validate real cluster/k8s/ config via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources).
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.health_checks import check_controller_health_checks


def test_no_dependency_errors(cluster: ParsedCluster, k8s_dir: Path) -> None:
    errors = validate_dependencies(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster, k8s_dir: Path, workspace: Path) -> None:
    errors = check_controller_health_checks(cluster, k8s_dir, workspace)
    assert not errors, "\n".join(errors)


def test_no_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> None:
    referenced: set[Path] = set()
    for kust in cluster.kustomize_files.values():
        referenced.update(kust.resources)
        referenced.update(kust.patches)
        for resource in kust.resources:
            if resource.is_dir():
                referenced.add(resource / "kustomization.yaml")

    orphaned = sorted(
        yaml_file.relative_to(k8s_dir)
        for yaml_file in cluster.all_yaml_files
        if yaml_file.name != "kustomization.yaml" and yaml_file not in referenced
    )
    assert not orphaned, "Orphaned files not referenced by any kustomization:\n" + "\n".join(f"  {f}" for f in orphaned)


if __name__ == "__main__":
    pytest_bazel.main()
