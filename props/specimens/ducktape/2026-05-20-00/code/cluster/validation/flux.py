"""Flux domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.tool_resolve import resolve_tool


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


class FluxKustomizationSpec(BaseModel):
    """Parsed spec from a Flux Kustomization CR."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    path: str = ""
    depends_on: list[DependsOn] = []
    health_checks: list[HealthCheck] = []
    retry_interval: str | None = None
    wait: bool = False
    suspend: bool = False

    def local_dir(self, k8s_dir: Path, k8s_subpath: str = "cluster/k8s") -> Path | None:
        """Resolve spec.path to a local directory under k8s_dir, or None if external."""
        rel = self.path.removeprefix("./")
        prefix = k8s_subpath + "/"
        if not rel.startswith(prefix):
            return None
        return (k8s_dir / rel[len(prefix) :]).resolve()


class _ObjectMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _FluxKustomizationDoc(BaseModel):
    """Top-level structure of a Flux Kustomization YAML document."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    metadata: _ObjectMeta
    spec: FluxKustomizationSpec = FluxKustomizationSpec()


def parse_flux_kustomizations(flux_file: Path) -> dict[str, FluxKustomizationSpec]:
    """Parse a flux-kustomization.yaml file, returning {name: spec} for each document."""
    results: dict[str, FluxKustomizationSpec] = {}
    with flux_file.open() as f:
        for doc in yaml.safe_load_all(f):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "Kustomization":
                continue
            if not (doc.get("apiVersion") or "").startswith("kustomize.toolkit.fluxcd.io"):
                continue
            parsed = _FluxKustomizationDoc.model_validate(doc)
            results[parsed.metadata.name] = parsed.spec

    return results


async def run_flux_build(k8s_dir: Path) -> tuple[int, str, str]:
    """Run flux build and return (returncode, stdout, stderr)."""
    kustomization_file = k8s_dir / "flux-system" / "gotk-sync.yaml"

    if not kustomization_file.exists():
        raise FileNotFoundError(f"gotk-sync.yaml not found at {kustomization_file}")

    flux_bin = resolve_tool("flux", "multitool/tools/flux/flux")
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
    return await proc.wait(), stdout_bytes.decode(), stderr_bytes.decode()


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
