"""Integration tests: validate real cluster/k8s/ config via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources,
blueprint completeness).

TODO: These checks duplicate validate_cluster (run via pre-commit). Consolidate by
enforcing affected bazel tests pass before commit (PR #819 WIP), then remove the
validate_cluster pre-commit hook and use this test as the single source of truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.scripts.validate_cluster.checks import check_goldilocks_namespace_labels
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


@pytest.fixture(scope="session")
def workspace(k8s_dir: Path) -> Path:
    """Repo root within runfiles (parent of cluster/k8s)."""
    return k8s_dir.parent.parent


@pytest.fixture(scope="session")
def cluster(k8s_dir: Path) -> ParsedCluster:
    return parse_cluster(k8s_dir)


def test_no_dependency_errors(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """No cycles, required dependencies present, operator dependencies satisfied."""
    errors = validate_dependencies(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster, k8s_dir: Path, workspace: Path) -> None:
    errors = check_controller_health_checks(cluster, k8s_dir, workspace)
    assert not errors, "\n".join(errors)


def test_retry_policy(cluster: ParsedCluster, k8s_dir: Path) -> None:
    check_retry_policy(cluster, k8s_dir)


@pytest.mark.xfail(reason="Pre-existing orphaned files (config.yaml referenced via configMapGenerator, not resources)")
def test_no_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """All YAML files must be referenced by a kustomization.yaml."""
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


def test_no_unwired_flux_kustomizations(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every flux-kustomization.yaml on disk must be referenced in the root kustomization."""
    on_disk = {f.resolve() for f in k8s_dir.rglob("flux-kustomization.yaml") if "flux-system" not in f.parts}

    root_kust = cluster.kustomize_files[k8s_dir / "kustomization.yaml"]
    referenced = {r for r in root_kust.resources if r.name == "flux-kustomization.yaml"}

    unwired = sorted(f.relative_to(k8s_dir) for f in on_disk - referenced)
    assert not unwired, "flux-kustomization.yaml files not listed in root kustomization.yaml:\n" + "\n".join(
        f"  {f}" for f in unwired
    )


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """Namespaces with goldilocks vpa-update-mode must also have goldilocks enabled."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_blueprint_completeness(k8s_dir: Path) -> None:
    """All authentik blueprint YAML files must be listed in configMapGenerator."""
    authentik_kust = k8s_dir / "authentik" / "kustomization.yaml"
    blueprints_dir = k8s_dir / "authentik" / "blueprints"

    if not authentik_kust.exists() or not blueprints_dir.exists():
        pytest.skip("Authentik kustomization or blueprints dir not found")

    with authentik_kust.open() as f:
        doc = yaml.safe_load(f)

    listed_files: set[str] = set()
    for generator in doc.get("configMapGenerator", []):
        if generator.get("name") == "authentik-sso-blueprints":
            listed_files = {Path(f).name for f in generator.get("files", [])}
            break

    on_disk = {p.name for p in blueprints_dir.glob("*.yaml")}
    unlisted = sorted(on_disk - listed_files)

    assert not unlisted, "Add to authentik-sso-blueprints files list: " + ", ".join(
        f"blueprints/{name}" for name in unlisted
    )


if __name__ == "__main__":
    pytest_bazel.main()
