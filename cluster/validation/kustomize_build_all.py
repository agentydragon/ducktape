"""Build all kustomizations and serialize results to JSON.

Runs kustomize build on all kustomization.yaml files found in the
cluster k8s directory, and writes the successful results as a JSON array.

Usage:
  bazel run //cluster/validation:kustomize_build_all -- <k8s-dir> <output-json>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
from pydantic import TypeAdapter

from cluster.validation.cluster import parse_cluster
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult
from util.bazel.runfiles import get_required_path


def _run_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult:
    """Run kustomize build and parse the output. Raises on failure."""
    kustomize_bin = get_required_path("multitool/tools/kustomize/kustomize")
    result = subprocess.run([kustomize_bin, "build", kustomization_path.parent], check=True, capture_output=True)
    resources = parse_k8s_resources(yaml.safe_load_all(result.stdout.decode()))
    return KustomizeBuildResult(kustomization_path=kustomization_path, resources=resources)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <k8s-dir> <output-json-path>", file=sys.stderr)
        return 1

    k8s_dir = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    cluster = parse_cluster(k8s_dir)

    kustomization_files = list(cluster.kustomize_files)
    if not kustomization_files:
        print(f"No kustomizations found in {k8s_dir}", file=sys.stderr)
        output_path.write_text("[]")
        return 0

    results = [_run_kustomize_build(k) for k in kustomization_files]

    # Relativize kustomization_path to k8s_dir for portability
    for r in results:
        r.kustomization_path = r.kustomization_path.relative_to(k8s_dir)

    adapter = TypeAdapter(list[KustomizeBuildResult])
    output_path.write_text(adapter.dump_json(results, indent=2).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
