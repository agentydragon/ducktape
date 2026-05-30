"""Kubernetes resource models and parsing utilities.

`parse_k8s_resources` is a small kind-discriminated parser: it returns the typed subclass
for the kinds that carry extra spec fields checks care about (HelmRelease, ImageRepository,
ImagePolicy, Receiver); every other kind stays as the generic `K8sResource` base. Consumers
isinstance-narrow to the variant they need.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class K8sMetadata(BaseModel):
    """Kubernetes resource metadata."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    namespace: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class K8sResource(BaseModel):
    """Parsed Kubernetes resource from YAML (generic base; see module docstring)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: str
    api_version: str = Field(default="", alias="apiVersion")
    metadata: K8sMetadata = Field(default_factory=K8sMetadata)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def namespace(self) -> str:
        return self.metadata.namespace


class HelmChartSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str | None = None


class HelmChart(BaseModel):
    model_config = ConfigDict(extra="ignore")
    spec: HelmChartSpec = Field(default_factory=HelmChartSpec)


class HelmReleaseSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chart: HelmChart = Field(default_factory=HelmChart)


class HelmReleaseResource(K8sResource):
    spec: HelmReleaseSpec = Field(default_factory=HelmReleaseSpec)

    @property
    def chart_version(self) -> str | None:
        return self.spec.chart.spec.version


class ImageRepositoryResource(K8sResource):
    """Flux `ImageRepository` — only its name is needed."""


class _ImageRepositoryRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class ImagePolicySpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    image_repository_ref: _ImageRepositoryRef = Field(default_factory=_ImageRepositoryRef)


class ImagePolicyResource(K8sResource):
    spec: ImagePolicySpec = Field(default_factory=ImagePolicySpec)


class ReceiverResourceRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = ""
    name: str = ""


class ReceiverSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resources: list[ReceiverResourceRef] = []


class ReceiverResource(K8sResource):
    spec: ReceiverSpec = Field(default_factory=ReceiverSpec)


_KIND_MODELS: dict[str, type[K8sResource]] = {
    "HelmRelease": HelmReleaseResource,
    "ImageRepository": ImageRepositoryResource,
    "ImagePolicy": ImagePolicyResource,
    "Receiver": ReceiverResource,
}


def parse_k8s_resources(docs: Iterable[object]) -> list[K8sResource]:
    """Parse YAML documents into K8s resources (typed subclass per kind), skipping non-resources."""
    return [
        _KIND_MODELS.get(doc["kind"], K8sResource).model_validate(doc)
        for doc in docs
        if isinstance(doc, dict) and doc.get("kind")
    ]


def parse_k8s_resource_file(yaml_file: Path) -> list[K8sResource]:
    """Parse a YAML file and extract all K8s resources."""
    with yaml_file.open() as f:
        return parse_k8s_resources(yaml.safe_load_all(f))
