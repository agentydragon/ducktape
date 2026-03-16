"""Flux runtime: build execution using flux binary."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import yaml

from cluster.validation.k8s import parse_k8s_resources
from util.bazel.runfiles import get_required_path


def run_flux_build(k8s_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run flux build and return the result."""
    kustomization_file = k8s_dir / "flux-system" / "gotk-sync.yaml"

    if not kustomization_file.exists():
        raise FileNotFoundError(f"gotk-sync.yaml not found at {kustomization_file}")

    flux_bin = get_required_path("multitool/tools/flux/flux")
    return subprocess.run(
        [
            flux_bin,
            "build",
            "kustomization",
            "flux-system",
            "--path",
            k8s_dir,
            "--kustomization-file",
            kustomization_file,
            "--dry-run",
            "--verbose",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def validate_flux_build(k8s_dir: Path) -> list[str]:
    """Validate flux build."""
    try:
        result = run_flux_build(k8s_dir)
    except FileNotFoundError as e:
        return [str(e)]

    if result.returncode != 0:
        return [f"flux build failed:\nk8s_dir: {k8s_dir}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"]

    if not result.stdout.strip():
        return [f"flux build returned empty output:\nk8s_dir: {k8s_dir}\nstderr: {result.stderr.strip() or 'none'}"]

    errors = []
    resource_counts: Counter[str] = Counter()

    for resource in parse_k8s_resources(yaml.safe_load_all(result.stdout)):
        resource_counts[resource.kind] += 1

    if resource_counts.get("Kustomization", 0) == 0:
        errors.append("No Flux Kustomization resources found in flux build output")
    if resource_counts.get("GitRepository", 0) == 0:
        errors.append("No GitRepository resource found in flux build output")

    return errors
