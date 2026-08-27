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
    annotations: dict[str, str] = Field(default_factory=dict)


class Condition(BaseModel):
    """A standard Kubernetes status condition.

    Used by controllers across the cluster (Flux Kustomizations, HelmReleases,
    Deployments via `Available`/`Progressing`, CNPG `Cluster.Ready`, etc.) to
    report observed state. Consumers usually filter by `type` and check
    `status` ("True"/"False"/"Unknown")."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    type: str
    status: str  # "True" / "False" / "Unknown"
    reason: str | None = None
    message: str | None = None
    last_transition_time: str | None = None
    observed_generation: int | None = None


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


class ImageRepositorySpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    image: str = ""


class ImageRepositoryResource(K8sResource):
    spec: ImageRepositorySpec = Field(default_factory=ImageRepositorySpec)


class TerraformBackendConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    custom_configuration: str = ""


class TerraformSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    backend_config: TerraformBackendConfig | None = None


class TerraformResource(K8sResource):
    spec: TerraformSpec = Field(default_factory=TerraformSpec)


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class FluxSourceRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = "GitRepository"
    name: str = ""
    namespace: str | None = None


class GitRepositorySpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    url: str = ""
    provider: str | None = None
    secret_ref: SecretRef | None = None


class GitRepositoryResource(K8sResource):
    spec: GitRepositorySpec = Field(default_factory=GitRepositorySpec)


class ImageUpdateAutomationSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    source_ref: FluxSourceRef = Field(default_factory=FluxSourceRef)


class ImageUpdateAutomationResource(K8sResource):
    spec: ImageUpdateAutomationSpec = Field(default_factory=ImageUpdateAutomationSpec)


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


class SopsMetadata(BaseModel):
    """The `sops:` block on a SOPS-encrypted document. Only presence is read by
    validation: a rendered Secret carrying it is ciphertext Flux cannot apply
    without a `decryption` block. Inner fields (mac, age recipients, lastmodified)
    are ignored — model them only if a future check consumes them."""

    model_config = ConfigDict(extra="ignore")


class SecretResource(K8sResource):
    """A `Secret`. `sops` is set when the source document carries a SOPS metadata
    block, i.e. the rendered Secret is still ENC[...] ciphertext."""

    sops: SopsMetadata | None = None


class CiliumRule(BaseModel):
    """One policy rule of a Cilium policy (`spec`, or an element of `specs`).

    Only the four rule-section lists are modeled — checks need their emptiness, not
    their contents."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    ingress: list[dict] = Field(default_factory=list)
    ingress_deny: list[dict] = Field(default_factory=list)
    egress: list[dict] = Field(default_factory=list)
    egress_deny: list[dict] = Field(default_factory=list)


class CiliumPolicyResource(K8sResource):
    """A `CiliumNetworkPolicy` or `CiliumClusterwideNetworkPolicy`."""

    spec: CiliumRule | None = None
    specs: list[CiliumRule] = Field(default_factory=list)

    @property
    def rules(self) -> list[CiliumRule]:
        return ([self.spec] if self.spec is not None else []) + self.specs


_KIND_MODELS: dict[str, type[K8sResource]] = {
    "GitRepository": GitRepositoryResource,
    "ImageUpdateAutomation": ImageUpdateAutomationResource,
    "HelmRelease": HelmReleaseResource,
    "Terraform": TerraformResource,
    "ImageRepository": ImageRepositoryResource,
    "ImagePolicy": ImagePolicyResource,
    "Receiver": ReceiverResource,
    "Secret": SecretResource,
    "CiliumNetworkPolicy": CiliumPolicyResource,
    "CiliumClusterwideNetworkPolicy": CiliumPolicyResource,
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
