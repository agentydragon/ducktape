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
8. Helm template rendering: Cilium values files render without errors (Bazel test only,
   enable in pre-commit with DUCKTAPE_HELM_VALIDATE=1)
9. Blueprint completeness: All authentik blueprint files must be listed in configMapGenerator
10. Proxy provider outpost assignment: every present authentik proxy provider must be assigned to an
    outpost, else its host 302s to a login flow served on itself and Google SSO redirect_uri_mismatch
11. Goldilocks explicit decision: Namespaces with workloads must explicitly set goldilocks enabled label
12. Image automation <-> webhook: every ImageRepository is listed in the GitHub webhook
    receiver (so pushes reconcile immediately, not just on the 5m poll), and every ImagePolicy
    references a defined ImageRepository
13. Terraform backends: tofu-controller Terraform CRs must not store state in Kubernetes Secrets

See AGENTS.md section "Flux Kustomization Layering" for CRD layering details.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cluster.validation.checks import (
    check_blueprint_completeness,
    check_duplicate_external_secrets,
    check_goldilocks_explicit_decision,
    check_goldilocks_namespace_labels,
    check_proxy_provider_outpost_assignment,
    find_orphaned_files,
)
from cluster.validation.cluster import parse_cluster
from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering
from cluster.validation.dependencies import validate_dependencies
from cluster.validation.flux import validate_flux_build
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from cluster.validation.health_checks import check_controller_health_checks
from cluster.validation.helm_templates import validate_helm_templates
from cluster.validation.image_automation import check_image_automation_webhook
from cluster.validation.kustomize import KustomizeBuildError, run_kustomize_build
from cluster.validation.terraform_backends import check_terraform_backends


async def validate(
    root: Path, *, skip_flux_build: bool = False, orphan_candidates: set[Path] | None = None
) -> list[str]:
    """Run all cluster validations. Returns a list of error strings.

    `orphan_candidates` scopes the orphaned-file check to a specific set of
    resolved file paths (pre-commit passes the staged file set). When None,
    every YAML under `root` is considered — the right behaviour for CI / the
    Bazel integration test.
    """
    cluster = parse_cluster(root)
    outcomes = await asyncio.gather(*[run_kustomize_build(k) for k in cluster.kustomize_files], return_exceptions=True)

    errors: list[str] = []

    for outcome in outcomes:
        if isinstance(outcome, KustomizeBuildError):
            errors.append(str(outcome))
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            cluster.build_results.append(outcome)

    errors.extend(check_duplicate_external_secrets(cluster.build_results))

    active_dirs = {spec.local_dir(root) for spec in cluster.active_flux_kustomizations.values()}
    for result in cluster.build_results:
        if result.kustomization_path.parent.resolve() not in active_dirs:
            continue
        try:
            check_crd_layering(result)
        except CrdLayeringViolationError as e:
            errors.append(str(e))

    errors.extend(find_orphaned_files(cluster, root, candidates=orphan_candidates))
    errors.extend(check_blueprint_completeness(root))
    errors.extend(check_proxy_provider_outpost_assignment(root))
    errors.extend(check_goldilocks_namespace_labels(cluster))
    errors.extend(check_goldilocks_explicit_decision(cluster))
    errors.extend(validate_dependencies(cluster, root))
    errors.extend(check_controller_health_checks(cluster, root))
    errors.extend(check_image_automation_webhook(cluster))
    errors.extend(check_flux_bootstrap_auth(cluster, root))
    errors.extend(check_terraform_backends(cluster))

    if not skip_flux_build:
        errors.extend(await validate_flux_build(root))

    if os.environ.get("DUCKTAPE_HELM_VALIDATE") == "1":
        errors.extend(await validate_helm_templates())

    return errors
