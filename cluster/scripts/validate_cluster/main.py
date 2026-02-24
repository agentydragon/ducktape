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
    check_controller_resource_health_checks,
    check_crd_layering,
    check_duplicate_external_secrets,
    find_orphaned_files,
)
from cluster.scripts.validate_cluster.cluster import _K8S_SUBPATH, parse_cluster
from cluster.scripts.validate_cluster.dependencies import validate_dependencies
from cluster.scripts.validate_cluster.flux import validate_flux_build
from cluster.scripts.validate_cluster.helm_templates import validate_helm_templates
from cluster.scripts.validate_cluster.kustomize import run_kustomize_build
from util.bazel.workspace import get_build_workspace_directory


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

    workspace = get_build_workspace_directory()
    root = args.root or (workspace / _K8S_SUBPATH)

    # Parse all files once
    cluster = parse_cluster(root)

    # Find kustomization.yaml files for build validation
    kustomization_files = list(cluster.kustomize_files)

    if not kustomization_files:
        print(f"No kustomizations found in {root}")
        return 0

    # Run kustomize build in parallel
    tasks = [run_kustomize_build(k) for k in kustomization_files]
    cluster.build_results = await asyncio.gather(*tasks)

    # Collect errors
    kust_errors: list[tuple[Path, str]] = []
    global_errors: list[str] = []

    successful = [r for r in cluster.build_results if r.success]
    for result in cluster.build_results:
        if not result.success:
            kust_errors.append((result.kustomization_path, result.error))

    # Check duplicate external-secrets
    global_errors.extend(check_duplicate_external_secrets(cluster.build_results))

    # Check CRD layering
    for result in cluster.build_results:
        for error in check_crd_layering(result):
            kust_errors.append((result.kustomization_path, error))

    # Check orphaned files
    global_errors.extend(find_orphaned_files(cluster, root))

    # Validate dependencies
    if not args.skip_dependencies:
        global_errors.extend(validate_dependencies(cluster, root))

    # Validate controller resource healthChecks (HelmRelease, Terraform)
    global_errors.extend(check_controller_resource_health_checks(cluster, root, workspace))

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
        result_json = {"status": "passed", "validated_count": str(len(successful))}
        print(json.dumps(result_json))
        return 0

    # Human-readable output
    if args.verbose and successful:
        print(f"✅ Successfully validated {len(successful)} kustomizations:")
        for r in successful:
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
        print(f"✅ All {len(successful)} kustomizations valid, no dependency/layering issues")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
