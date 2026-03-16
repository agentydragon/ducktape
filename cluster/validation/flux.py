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
    """Spec portion of a Flux Kustomization CR."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    path: str = ""
    depends_on: list[DependsOn] = []
    health_checks: list[HealthCheck] = []


class FluxKustomization(BaseModel):
    """Parsed flux-kustomization.yaml Kustomization CR."""

    name: str
    file_path: Path
    spec: FluxKustomizationSpec = FluxKustomizationSpec()


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
            if not (name := (doc.get("metadata") or {}).get("name")):
                continue

            spec = FluxKustomizationSpec.model_validate(doc.get("spec") or {})
            results.append(FluxKustomization(name=name, file_path=flux_file, spec=spec))

    return results
