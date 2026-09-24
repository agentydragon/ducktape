"""Kubernetes resource models and parsing utilities.

`parse_k8s_resources` is a small kind-discriminated parser: it returns the typed subclass
for the kinds checks care about (ImageRepository, ImagePolicy, Receiver, secret stores, and
selected workloads); every other kind stays as the generic `K8sResource` base. Consumers
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


class PodContainer(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    image: str | None = None


class ContainerDisk(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    image: str | None = None


class PodVolume(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    container_disk: ContainerDisk | None = None


class PodSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    containers: list[PodContainer] = Field(default_factory=list)
    init_containers: list[PodContainer] = Field(default_factory=list)
    volumes: list[PodVolume] = Field(default_factory=list)

    @property
    def images(self) -> list[str]:
        images = [container.image for container in [*self.containers, *self.init_containers] if container.image]
        images.extend(
            volume.container_disk.image
            for volume in self.volumes
            if volume.container_disk and volume.container_disk.image
        )
        return images


class PodTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    spec: PodSpec = Field(default_factory=PodSpec)


class PodTemplateWorkloadSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    template: PodTemplateSpec = Field(default_factory=PodTemplateSpec)


class PodTemplateWorkloadResource(K8sResource):
    spec: PodTemplateWorkloadSpec = Field(default_factory=PodTemplateWorkloadSpec)

    @property
    def pod_specs(self) -> list[PodSpec]:
        return [self.spec.template.spec]


class CronJobJobTemplate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    template: PodTemplateSpec = Field(default_factory=PodTemplateSpec)


class CronJobSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    job_template: CronJobJobTemplate = Field(default_factory=CronJobJobTemplate)


class CronJobResource(K8sResource):
    spec: CronJobSpec = Field(default_factory=CronJobSpec)

    @property
    def pod_specs(self) -> list[PodSpec]:
        return [self.spec.job_template.template.spec]


class SandboxTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    pod_template: PodTemplateSpec = Field(default_factory=PodTemplateSpec)


class SandboxTemplateResource(K8sResource):
    spec: SandboxTemplateSpec = Field(default_factory=SandboxTemplateSpec)

    @property
    def pod_specs(self) -> list[PodSpec]:
        return [self.spec.pod_template.spec]


class ImageRepositorySpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    image: str = ""


class ImageRepositoryResource(K8sResource):
    spec: ImageRepositorySpec = Field(default_factory=ImageRepositorySpec)


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class RoleBindingSubject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""


class RoleBindingResource(K8sResource):
    subjects: list[RoleBindingSubject] = Field(default_factory=list)


class EgressBindingSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policies: list[str] = Field(default_factory=list)


class EgressBindingResource(K8sResource):
    spec: EgressBindingSpec = Field(default_factory=EgressBindingSpec)


class SecretStoreKubernetesProvider(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    remote_namespace: str = ""


class SecretStoreProvider(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kubernetes: SecretStoreKubernetesProvider | None = None


class SecretStoreCondition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespaces: list[str] = Field(default_factory=list)


class SecretStoreSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conditions: list[SecretStoreCondition] = Field(default_factory=list)
    provider: SecretStoreProvider = Field(default_factory=SecretStoreProvider)


class SecretStoreResource(K8sResource):
    spec: SecretStoreSpec = Field(default_factory=SecretStoreSpec)


class ExternalSecretStoreRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = "SecretStore"
    name: str = ""


class ExternalSecretSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    secret_store_ref: ExternalSecretStoreRef = Field(default_factory=ExternalSecretStoreRef)


class ExternalSecretResource(K8sResource):
    spec: ExternalSecretSpec = Field(default_factory=ExternalSecretSpec)


class GitRepositorySpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    url: str = ""
    provider: str | None = None
    secret_ref: SecretRef | None = None


class GitRepositoryResource(K8sResource):
    spec: GitRepositorySpec = Field(default_factory=GitRepositorySpec)


class ImagePolicyResource(K8sResource):
    """An image-automation `ImagePolicy`: what a `$imagepolicy` setter marker names."""


class _ArtifactGeneratorArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str


class ArtifactGeneratorSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    artifacts: list[_ArtifactGeneratorArtifact] = Field(default_factory=list)


class ArtifactGeneratorResource(K8sResource):
    """source-watcher `ArtifactGenerator`: each `spec.artifacts[].name` is an `ExternalArtifact` the
    controller creates in the generator's namespace -- a Kustomization sourceRef target that never
    appears in Git itself."""

    spec: ArtifactGeneratorSpec = Field(default_factory=ArtifactGeneratorSpec)


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
    "ImageRepository": ImageRepositoryResource,
    "ImagePolicy": ImagePolicyResource,
    "ArtifactGenerator": ArtifactGeneratorResource,
    "Receiver": ReceiverResource,
    "RoleBinding": RoleBindingResource,
    "Secret": SecretResource,
    "SecretStore": SecretStoreResource,
    "ClusterSecretStore": SecretStoreResource,
    "ExternalSecret": ExternalSecretResource,
    "CiliumNetworkPolicy": CiliumPolicyResource,
    "CiliumClusterwideNetworkPolicy": CiliumPolicyResource,
    "CronJob": CronJobResource,
    "DaemonSet": PodTemplateWorkloadResource,
    "Deployment": PodTemplateWorkloadResource,
    "EgressBinding": EgressBindingResource,
    "Job": PodTemplateWorkloadResource,
    "SandboxTemplate": SandboxTemplateResource,
    "StatefulSet": PodTemplateWorkloadResource,
    "VirtualMachine": PodTemplateWorkloadResource,
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
