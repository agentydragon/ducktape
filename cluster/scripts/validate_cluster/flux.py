"""Flux domain: models, parsing, and build execution."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cluster.scripts.validate_cluster.k8s import parse_k8s_resources
from util.bazel.runfiles import get_required_path


class DependsOn(BaseModel):
    """Flux Kustomization dependency reference."""

    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str | None = None


class HealthCheck(BaseModel):
    """Flux Kustomization health check reference."""

    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    name: str = ""
    namespace: str = ""


class FluxKustomization(BaseModel):
    """Parsed flux-kustomization.yaml Kustomization CR."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    file_path: Path
    spec_path: str = Field(default="", alias="path")
    depends_on: list[DependsOn] = Field(default=[], alias="dependsOn")
    health_checks: list[HealthCheck] = Field(default=[], alias="healthChecks")


def parse_flux_kustomization(flux_file: Path) -> list[FluxKustomization]:
    """Parse a flux-kustomization.yaml file (may contain multiple documents)."""
    results = []
    with flux_file.open() as f:
        for doc in yaml.safe_load_all(f):
            if not doc:
                continue
            if doc.get("kind") != "Kustomization":
                continue
            if not doc.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io"):
                continue

            metadata = doc.get("metadata", {}) or {}
            name = metadata.get("name", "")
            if not name:
                continue

            spec = doc.get("spec", {}) or {}
            results.append(FluxKustomization.model_validate({"name": name, "file_path": flux_file, **spec}))

    return results


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
