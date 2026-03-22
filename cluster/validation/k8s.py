"""Kubernetes resource models and parsing utilities."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class K8sMetadata(BaseModel):
    """Kubernetes resource metadata."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    namespace: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class HelmChartSpec(BaseModel):
    """HelmRelease chart spec."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None


class HelmChart(BaseModel):
    """HelmRelease chart reference."""

    model_config = ConfigDict(extra="ignore")

    spec: HelmChartSpec = Field(default_factory=HelmChartSpec)


class HelmReleaseSpec(BaseModel):
    """HelmRelease spec."""

    model_config = ConfigDict(extra="ignore")

    chart: HelmChart = Field(default_factory=HelmChart)


class K8sResource(BaseModel):
    """Parsed Kubernetes resource from YAML."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: str
    api_version: str = Field(default="", alias="apiVersion")
    metadata: K8sMetadata = Field(default_factory=K8sMetadata)
    spec: HelmReleaseSpec | None = None

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def namespace(self) -> str:
        return self.metadata.namespace

    @property
    def chart_version(self) -> str | None:
        if self.kind != "HelmRelease" or not self.spec:
            return None
        return self.spec.chart.spec.version


def _is_k8s_resource_doc(doc: object) -> bool:
    """Check if a YAML document looks like a K8s resource (dict with kind)."""
    return isinstance(doc, dict) and bool(doc.get("kind"))


def parse_k8s_resources(docs: Iterable[object]) -> list[K8sResource]:
    """Parse an iterable of YAML documents into K8s resources, skipping non-resource documents."""
    return [K8sResource.model_validate(doc) for doc in docs if _is_k8s_resource_doc(doc)]


def parse_k8s_resource_file(yaml_file: Path) -> list[K8sResource]:
    """Parse a YAML file and extract all K8s resources."""
    with yaml_file.open() as f:
        return parse_k8s_resources(yaml.safe_load_all(f))
