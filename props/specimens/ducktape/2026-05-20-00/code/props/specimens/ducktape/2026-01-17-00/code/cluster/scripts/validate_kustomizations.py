"""Parallel kustomize validation script.

Validates all kustomizations quickly and quietly (unless errors occur).

Run via Bazel: bazel run //cluster/scripts:validate_kustomizations
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from cluster.scripts.runfiles_util import resolve_path

_KUSTOMIZE_BIN = resolve_path("multitool/tools/kustomize/kustomize")


async def validate_kustomization(kustomization_path: Path) -> tuple[Path, bool, str]:
    """Validate a single kustomization directory."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _KUSTOMIZE_BIN,
            "build",
            kustomization_path.parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            return kustomization_path, True, stdout.decode()
        return kustomization_path, False, stderr.decode()
    except Exception as e:
        return kustomization_path, False, str(e)


async def main():
    parser = argparse.ArgumentParser(description="Validate kustomizations in parallel")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show successful validations")
    parser.add_argument("--root", default="k8s/", help="Root directory to search for kustomizations")
    parser.add_argument(
        "--format", choices=["human", "json"], default="human", help="Output format (human or json for Terraform)"
    )
    args = parser.parse_args()

    # Find all kustomization.yaml files (excluding flux-system)
    root = Path(args.root)
    kustomizations = []

    for kustomization_file in root.rglob("kustomization.yaml"):
        if "flux-system" not in kustomization_file.parts:
            kustomizations.append(kustomization_file)

    if not kustomizations:
        print(f"No kustomizations found in {root}")
        return 0

    # Validate all kustomizations in parallel
    tasks = [validate_kustomization(k) for k in kustomizations]
    results = await asyncio.gather(*tasks)

    # Process results
    successful = []
    failed = []
    kustomize_outputs = {}

    for kustomization, success, output in results:
        if success:
            successful.append(kustomization)
            kustomize_outputs[kustomization] = output
        else:
            failed.append((kustomization, output))

    # Check for duplicate external-secrets installations
    external_secrets_deployments = defaultdict(list)

    for kustomization, output in kustomize_outputs.items():
        try:
            documents = yaml.safe_load_all(output)
            for doc in documents:
                if (
                    doc
                    and doc.get("kind") == "HelmRelease"
                    and doc.get("metadata", {}).get("name") == "external-secrets"
                ):
                    namespace = doc.get("metadata", {}).get("namespace", "default")
                    chart_version = doc.get("spec", {}).get("chart", {}).get("spec", {}).get("version", "unknown")
                    external_secrets_deployments[f"{namespace}/{chart_version}"].append(str(kustomization.parent))
        except Exception:
            # Ignore YAML parsing errors for duplicate check
            pass

    # Validate exactly one external-secrets installation
    duplicate_errors = []
    if len(external_secrets_deployments) > 1:
        duplicate_errors.append("Multiple external-secrets HelmRelease found:")
        for deployment, paths in external_secrets_deployments.items():
            duplicate_errors.append(f"  {deployment}: {', '.join(paths)}")
        duplicate_errors.append("There should be exactly ONE external-secrets installation.")
    elif len(external_secrets_deployments) == 0:
        duplicate_errors.append("No external-secrets HelmRelease found. At least one is required.")

    if duplicate_errors:
        error_msg = "\n".join(duplicate_errors)
        failed.append((Path("external-secrets-validation"), error_msg))

    # Output results
    if args.format == "json":
        # JSON output for Terraform data source
        if failed:
            error_details = [{"path": str(k.parent), "error": error.strip()} for k, error in failed]
            result = {"error": f"Failed to validate {len(failed)} kustomizations", "details": error_details}
            print(json.dumps(result), file=sys.stderr)
            return 1
        result = {"status": "passed", "validated_count": str(len(successful))}
        print(json.dumps(result))
        return 0
    # Human-readable output
    if args.verbose and successful:
        print(f"✅ Successfully validated {len(successful)} kustomizations:")
        for k in successful:
            print(f"  {k.parent}")

    if failed:
        print(f"❌ Failed to validate {len(failed)} kustomizations:")
        for kustomization, error in failed:
            print(f"  {kustomization.parent}:")
            print(f"    {error.strip()}")
        return 1

    if not args.verbose:
        print(f"✅ All {len(successful)} kustomizations valid")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
