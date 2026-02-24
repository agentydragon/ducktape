"""Kustomize domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from pydantic import BaseModel

from cluster.scripts.validate_cluster.k8s import K8sResource, parse_k8s_resources
from util.bazel.runfiles import get_required_path


class KustomizeFile(BaseModel):
    """Parsed kustomization.yaml file."""

    path: Path
    resources: list[Path] = []  # Resolved absolute paths
    patches: list[Path] = []  # Resolved absolute paths (from patches: and patchesStrategicMerge:)


class KustomizeBuildResult(BaseModel):
    """Result of running kustomize build on a directory."""

    kustomization_path: Path
    success: bool
    error: str = ""
    resources: list[K8sResource] = []


def parse_kustomize_file(kust_file: Path) -> KustomizeFile | None:
    """Parse a kustomization.yaml file. Returns None only for empty files."""
    with kust_file.open() as f:
        doc = yaml.safe_load(f)
        if not doc:
            return None

        resources: list[Path] = []
        patches: list[Path] = []

        # Parse resources:
        for resource in doc.get("resources", []):
            resource_path = (kust_file.parent / resource).resolve()
            resources.append(resource_path)

        # Parse patches: (new format with path key)
        for patch in doc.get("patches", []):
            if isinstance(patch, dict) and "path" in patch:
                patch_path = (kust_file.parent / patch["path"]).resolve()
                patches.append(patch_path)

        # Parse patchesStrategicMerge: (legacy format)
        for patch in doc.get("patchesStrategicMerge", []):
            if isinstance(patch, str):
                patch_path = (kust_file.parent / patch).resolve()
                patches.append(patch_path)

        return KustomizeFile(path=kust_file, resources=resources, patches=patches)


async def run_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult:
    """Run kustomize build and parse the output."""
    kustomize_bin = get_required_path("multitool/tools/kustomize/kustomize")
    proc = await asyncio.create_subprocess_exec(
        kustomize_bin,
        "build",
        kustomization_path.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return KustomizeBuildResult(kustomization_path=kustomization_path, success=False, error=stderr.decode())

    output = stdout.decode()
    resources = parse_k8s_resources(yaml.safe_load_all(output))

    return KustomizeBuildResult(kustomization_path=kustomization_path, success=True, resources=resources)
