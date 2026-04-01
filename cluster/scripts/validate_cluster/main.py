"""Unified cluster validation: kustomizations + helm templates.

Validates all kustomizations and helm templates quickly and quietly (unless errors occur).

Checks:
1. kustomize build succeeds for each kustomization
2. No duplicate external-secrets installations
3. CRD layering: HelmReleases are not mixed with CRD instances from external operators
4. Orphaned files: YAML files must be referenced by a kustomization.yaml
5. Dependency graph: No circular dependencies, required dependencies present
6. Flux build: Validates flux can build the complete kustomization tree
7. Controller resource healthChecks: Flux kustomizations deploying HelmReleases or Terraform CRs
   must have healthChecks for them
8. Helm template rendering: Cilium values files render without errors
9. Blueprint completeness: All authentik blueprint files must be listed in configMapGenerator
10. Goldilocks explicit decision: Namespaces with workloads must explicitly set goldilocks enabled label

See AGENTS.md section "Flux Kustomization Layering" for CRD layering details.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cluster.scripts.validate_cluster.checks import (
    check_blueprint_completeness,
    check_crd_layering,
    check_duplicate_external_secrets,
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    find_orphaned_files,
)
from cluster.scripts.validate_cluster.flux import validate_flux_build
from cluster.scripts.validate_cluster.helm_templates import validate_helm_templates
from cluster.scripts.validate_cluster.kustomize import KustomizeBuildError, run_kustomize_build
from cluster.validation.cluster import parse_cluster
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.health_checks import check_controller_health_checks
from cluster.validation.kustomize import KustomizeBuildResult


async def _try_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult | KustomizeBuildError:
    """Run kustomize build, returning the result or error (not raising)."""
    try:
        return await run_kustomize_build(kustomization_path)
    except KustomizeBuildError as e:
        return e


async def validate(
    root: Path, *, skip_flux_build: bool = False, skip_dependencies: bool = False
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Run all cluster validations.

    Returns (kust_errors, global_errors) where kust_errors are
    (kustomization_path, error_message) pairs.
    """
    cluster = parse_cluster(root)
    kustomization_files = list(cluster.kustomize_files)

    if not kustomization_files:
        return [], []

    outcomes = await asyncio.gather(*[_try_kustomize_build(k) for k in kustomization_files])

    kust_errors: list[tuple[Path, str]] = []
    global_errors: list[str] = []

    for outcome in outcomes:
        if isinstance(outcome, KustomizeBuildError):
            kust_errors.append((outcome.kustomization_path, str(outcome)))
        else:
            cluster.build_results.append(outcome)

    global_errors.extend(check_duplicate_external_secrets(cluster.build_results))

    active_dirs = {spec.local_dir(root) for spec in cluster.active_flux_kustomizations.values()}
    for result in cluster.build_results:
        if result.kustomization_path.parent.resolve() not in active_dirs:
            continue
        kust_errors.extend((result.kustomization_path, error) for error in check_crd_layering(result))

    global_errors.extend(find_orphaned_files(cluster, root))
    global_errors.extend(check_blueprint_completeness(root))
    global_errors.extend(check_goldilocks_namespace_labels(cluster))
    global_errors.extend(check_goldilocks_explicit_decision(cluster))

    if not skip_dependencies:
        global_errors.extend(validate_dependencies(cluster, root))

    global_errors.extend(check_controller_health_checks(cluster, root))

    if not skip_flux_build:
        global_errors.extend(await validate_flux_build(root))

    global_errors.extend(await validate_helm_templates())

    return kust_errors, global_errors
