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

Run via Bazel: bazel run //cluster/scripts/validate_cluster
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
from util.bazel.workspace import get_build_workspace_directory


async def _try_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult | KustomizeBuildError:
    """Run kustomize build, returning the result or error (not raising)."""
    try:
        return await run_kustomize_build(kustomization_path)
    except KustomizeBuildError as e:
        return e


async def main() -> int:
    parser = argparse.ArgumentParser(description="Validate kustomizations in parallel")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show successful validations")
    parser.add_argument("--root", type=Path, help="Root directory to search for kustomizations")
    parser.add_argument(
        "--format", choices=["human", "json"], default="human", help="Output format (human or json for Terraform)"
    )
    parser.add_argument(
        "--skip-flux-build",
        action="store_true",
        help="Skip flux build validation (useful when flux-system not bootstrapped)",
    )
    parser.add_argument("--skip-dependencies", action="store_true", help="Skip dependency graph validation")
    args = parser.parse_args()

    root = args.root or (get_build_workspace_directory() / "cluster/k8s")

    # Parse all files once
    cluster = parse_cluster(root)

    # Find kustomization.yaml files for build validation
    kustomization_files = list(cluster.kustomize_files)

    if not kustomization_files:
        print(f"No kustomizations found in {root}")
        return 0

    # Run kustomize build in parallel — collect successes and failures
    outcomes = await asyncio.gather(*[_try_kustomize_build(k) for k in kustomization_files])

    kust_errors: list[tuple[Path, str]] = []
    global_errors: list[str] = []

    for outcome in outcomes:
        if isinstance(outcome, KustomizeBuildError):
            kust_errors.append((outcome.kustomization_path, str(outcome)))
        else:
            cluster.build_results.append(outcome)

    # Check duplicate external-secrets
    global_errors.extend(check_duplicate_external_secrets(cluster.build_results))

    # Check CRD layering (only active flux kustomizations — suspended are excluded
    # by flux_kust_resources which filters via active_flux_kustomizations).
    active_dirs = {spec.local_dir(root) for spec in cluster.active_flux_kustomizations.values()}
    for result in cluster.build_results:
        if result.kustomization_path.parent.resolve() not in active_dirs:
            continue
        kust_errors.extend((result.kustomization_path, error) for error in check_crd_layering(result))

    # Check orphaned files
    global_errors.extend(find_orphaned_files(cluster, root))

    # Check authentik blueprint completeness
    global_errors.extend(check_blueprint_completeness(root))

    # Check goldilocks namespace labels
    global_errors.extend(check_goldilocks_namespace_labels(cluster))

    # Check goldilocks explicit decision on workload namespaces
    global_errors.extend(check_goldilocks_explicit_decision(cluster))

    # Validate dependencies
    if not args.skip_dependencies:
        global_errors.extend(validate_dependencies(cluster, root))

    # Validate controller resource healthChecks (HelmRelease, Terraform)
    global_errors.extend(check_controller_health_checks(cluster, root))

    # Validate flux build
    if not args.skip_flux_build:
        global_errors.extend(validate_flux_build(root))

    # Validate Helm templates
    global_errors.extend(validate_helm_templates())

    has_errors = bool(kust_errors or global_errors)

    # Output results
    if args.format == "json":
        if has_errors:
            error_details = [{"path": str(k.parent), "error": error.strip()} for k, error in kust_errors]
            error_details.extend([{"path": "", "error": error.strip()} for error in global_errors])
            result_json = {"error": f"Validation failed with {len(error_details)} errors", "details": error_details}
            print(json.dumps(result_json), file=sys.stderr)
            return 1
        result_json = {"status": "passed", "validated_count": str(len(cluster.build_results))}
        print(json.dumps(result_json))
        return 0

    # Human-readable output
    if args.verbose and cluster.build_results:
        print(f"✅ Successfully validated {len(cluster.build_results)} kustomizations:")
        for r in cluster.build_results:
            print(f"  {r.kustomization_path.parent}")

    if has_errors:
        print("❌ Validation failed:")
        for kustomization, error in kust_errors:
            print(f"  {kustomization.parent}:")
            print(f"    {error.strip()}")
        for error in global_errors:
            print(f"  {error.strip()}")
        return 1

    if not args.verbose:
        print(f"✅ All {len(cluster.build_results)} kustomizations valid, no dependency/layering issues")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
