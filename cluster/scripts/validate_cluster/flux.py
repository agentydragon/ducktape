"""Flux runtime: build execution using flux binary."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import yaml

from cluster.validation.k8s import parse_k8s_resources
from util.bazel.runfiles import get_required_path


async def run_flux_build(k8s_dir: Path) -> tuple[int, str, str]:
    """Run flux build and return (returncode, stdout, stderr)."""
    kustomization_file = k8s_dir / "flux-system" / "gotk-sync.yaml"

    if not kustomization_file.exists():
        raise FileNotFoundError(f"gotk-sync.yaml not found at {kustomization_file}")

    flux_bin = get_required_path("multitool/tools/flux/flux")
    proc = await asyncio.create_subprocess_exec(
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode is not None
    return proc.returncode, stdout_bytes.decode(), stderr_bytes.decode()


async def validate_flux_build(k8s_dir: Path) -> list[str]:
    """Validate flux build."""
    try:
        returncode, stdout, stderr = await run_flux_build(k8s_dir)
    except FileNotFoundError as e:
        return [str(e)]

    if returncode != 0:
        return [f"flux build failed:\nk8s_dir: {k8s_dir}\nstdout:\n{stdout}\nstderr:\n{stderr}"]

    if not stdout.strip():
        return [f"flux build returned empty output:\nk8s_dir: {k8s_dir}\nstderr: {stderr.strip() or 'none'}"]

    errors = []
    resource_counts: Counter[str] = Counter()

    for resource in parse_k8s_resources(yaml.safe_load_all(stdout)):
        resource_counts[resource.kind] += 1

    if resource_counts.get("Kustomization", 0) == 0:
        errors.append("No Flux Kustomization resources found in flux build output")
    if resource_counts.get("GitRepository", 0) == 0:
        errors.append("No GitRepository resource found in flux build output")

    return errors
