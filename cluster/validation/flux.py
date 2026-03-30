"""Flux domain: models and parsing."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


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
