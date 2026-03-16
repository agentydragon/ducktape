"""Kustomize domain: models and parsing."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from cluster.validation.k8s import K8sResource


class KustomizeFile(BaseModel):
    """Parsed kustomization.yaml file."""

    path: Path
    resources: list[Path] = []  # Resolved absolute paths
    patches: list[Path] = []  # Resolved absolute paths


class KustomizeBuildResult(BaseModel):
    """Successful kustomize build output for a single kustomization directory."""

    kustomization_path: Path
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

        if "patchesStrategicMerge" in doc:
            raise ValueError(
                f"{kust_file}: uses deprecated 'patchesStrategicMerge'. "
                "Convert to 'patches' format (list of {{path: ...}} objects)."
            )

        return KustomizeFile(path=kust_file, resources=resources, patches=patches)
