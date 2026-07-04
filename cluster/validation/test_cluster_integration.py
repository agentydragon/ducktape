"""Integration tests: validate real cluster/k8s/ config via pure analysis.

Tests that parse the cluster kustomization tree and check structural invariants
(no orphaned files, valid dependencies, health checks on controller resources,
blueprint completeness). All kustomizations are built with kustomize to validate
they render correctly and to provide build results for resource-level checks.

These Bazel tests are the single source of truth for cluster validation; the
former `cluster-validate` pre-commit hook was removed in favor of running them
(and the sibling `test_*.py` targets) in CI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.authentik_blueprints import (
    check_blueprint_completeness,
    check_proxy_provider_outpost_assignment,
)
from cluster.validation.checks import (
    check_duplicate_external_secrets,
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    check_sops_decryption_blocks,
    find_orphaned_files,
)
from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.image_automation import check_image_automation_webhook
from cluster.validation.kustomize import KustomizeBuildResult, run_kustomize_build
from cluster.validation.terraform_backends import check_terraform_backends
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


def test_no_crd_layering_violations(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Active kustomizations must not mix HelmReleases with external-operator CRD instances."""
    active_dirs = {spec.local_dir(k8s_dir) for spec in cluster.active_flux_kustomizations.values()}
    errors: list[str] = []
    for result in cluster.build_results:
        if result.kustomization_path.parent.resolve() not in active_dirs:
            continue
        try:
            check_crd_layering(result)
        except CrdLayeringViolationError as e:
            errors.append(str(e))
    assert not errors, "\n".join(errors)


def test_single_external_secrets_installation(cluster: ParsedCluster) -> None:
    """Exactly one external-secrets HelmRelease across the cluster."""
    errors = check_duplicate_external_secrets(cluster.build_results)
    assert not errors, "\n".join(errors)


def test_terraform_backends_not_kubernetes(cluster: ParsedCluster) -> None:
    """tofu-controller Terraform CRs must use the pg backend, not kubernetes Secrets."""
    errors = check_terraform_backends(cluster)
    assert not errors, "\n".join(errors)


def test_image_automation_webhook_consistency(cluster: ParsedCluster) -> None:
    """Every rendered ImageRepository is in the webhook Receiver; every ImagePolicy ref resolves.

    Runs against the real built cluster (not synthetic fixtures), so it also guards the
    check against crashing on the actual manifest set — the gap that hid the earlier
    raw-YAML-walking bug.
    """
    errors = check_image_automation_webhook(cluster)
    assert not errors, "\n".join(errors)


def test_flux_bootstrap_auth_split(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Cold bootstrap sources must not depend on Flux-decrypted auth; write sources must."""
    errors = check_flux_bootstrap_auth(cluster, k8s_dir)
    assert not errors, "\n".join(errors)


def test_sops_secrets_have_decryption_block(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Active flux kustomizations rendering a SOPS Secret must declare decryption.provider: sops."""
    errors = check_sops_decryption_blocks(cluster, k8s_dir)
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
    errors = check_blueprint_completeness(k8s_dir)
    assert not errors, "\n".join(errors)


def test_proxy_providers_assigned_to_outpost(k8s_dir: Path) -> None:
    """Every present authentik proxy provider must be assigned to an outpost.

    An unassigned proxy provider (HTTPRoute present, but not on the embedded outpost)
    302s to a login flow served on its own host, breaking Google SSO with
    redirect_uri_mismatch — the haku.allegedly.works failure mode.
    """
    errors = check_proxy_provider_outpost_assignment(k8s_dir)
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
