"""Integration tests: validate real cluster/k8s/ config via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources,
blueprint completeness). All kustomizations are built with kustomize to validate
they render correctly and to provide build results for resource-level checks.

TODO: These checks duplicate validate_cluster (run via pre-commit). Consolidate by
enforcing affected bazel tests pass before commit (PR #819 WIP), then remove the
validate_cluster pre-commit hook and use this test as the single source of truth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.scripts.validate_cluster.checks import (
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    find_orphaned_files,
)
from cluster.scripts.validate_cluster.kustomize import run_kustomize_build
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.kustomize import KustomizeBuildResult
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def _local_flux_kust_names(parsed: ParsedCluster, k8s_dir: Path) -> set[str]:
    """Active flux kustomization names whose spec.path points into the local cluster/k8s tree."""
    return {name for name, spec in parsed.active_flux_kustomizations.items() if spec.local_dir(k8s_dir)}


@pytest.fixture(scope="session")
def cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse cluster and build flux-referenced kustomizations (hard failure on any build error)."""
    parsed = parse_cluster(k8s_dir)

    # Build all local flux-referenced kustomizations (including suspended — kustomize
    # build should still succeed). Only validation checks filter suspended.
    local_dirs = {d for spec in parsed.flux_kustomizations.values() if (d := spec.local_dir(k8s_dir))}
    kust_files = [k for k in parsed.kustomize_files if k.parent.resolve() in local_dirs]

    async def _build_all() -> list[KustomizeBuildResult]:
        return list(await asyncio.gather(*[run_kustomize_build(k) for k in kust_files]))

    parsed.build_results = asyncio.run(_build_all())
    return parsed


def test_all_local_flux_kustomizations_have_build_results(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every flux kustomization pointing to a local path must have a build result."""
    covered = set(cluster.flux_kust_resources(k8s_dir))
    expected = _local_flux_kust_names(cluster, k8s_dir)
    missing = sorted(expected - covered)
    assert not missing, "Flux kustomizations with no build result:\n" + "\n".join(f"  {m}" for m in missing)


def test_no_dependency_errors(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """No cycles, required dependencies present, operator dependencies satisfied."""
    errors = validate_dependencies(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_controller_resources_have_health_checks(cluster: ParsedCluster, k8s_dir: Path) -> None:
    errors = check_controller_health_checks(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_retry_policy(cluster: ParsedCluster) -> None:
    check_retry_policy(cluster)


def test_no_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """All YAML files must be referenced by a kustomization.yaml."""
    errors = find_orphaned_files(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_no_unwired_flux_kustomizations(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Every flux-kustomization.yaml on disk must be referenced in the root kustomization."""
    on_disk = {f.resolve() for f in k8s_dir.rglob("flux-kustomization.yaml") if "flux-system" not in f.parts}

    root_kust = cluster.kustomize_files[k8s_dir / "kustomization.yaml"]
    referenced = {r for r in root_kust.resolved_resources if r.name == "flux-kustomization.yaml"}

    unwired = sorted(f.relative_to(k8s_dir) for f in on_disk - referenced)
    assert not unwired, "flux-kustomization.yaml files not listed in root kustomization.yaml:\n" + "\n".join(
        f"  {f}" for f in unwired
    )


def test_goldilocks_namespace_labels(cluster: ParsedCluster) -> None:
    """Namespaces with goldilocks vpa-update-mode must also have goldilocks enabled."""
    errors = check_goldilocks_namespace_labels(cluster)
    assert not errors, "\n".join(errors)


def test_goldilocks_explicit_decision(cluster: ParsedCluster) -> None:
    """Namespaces with workloads must explicitly set goldilocks enabled label."""
    errors = check_goldilocks_explicit_decision(cluster)
    assert not errors, "\n".join(errors)


def test_blueprint_completeness(k8s_dir: Path) -> None:
    """All authentik blueprint YAML files must be listed in configMapGenerator."""
    authentik_kust = k8s_dir / "authentik" / "app" / "kustomization.yaml"
    blueprints_dir = k8s_dir / "authentik" / "app" / "blueprints"

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
