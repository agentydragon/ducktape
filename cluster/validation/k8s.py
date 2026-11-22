"""Kubernetes resource models and parsing utilities.

`parse_k8s_resources` selects typed views for resources inspected by validation checks.
SandboxTemplate dispatch also uses the API group because the two groups have different shapes.
Other resources use the generic `K8sResource` base.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NotRequired, TypedDict

import yaml
from pydantic import AliasPath, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelCaseModel(BaseModel):
    """Accept Kubernetes camelCase fields and Python field names in validation models."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class K8sMetadata(BaseModel):
    """Kubernetes resource metadata."""

    name: str = ""
    namespace: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class Condition(CamelCaseModel):
    """A standard Kubernetes status condition.

    Used by controllers across the cluster (Flux Kustomizations, HelmReleases,
    Deployments via `Available`/`Progressing`, CNPG `Cluster.Ready`, etc.) to
    report observed state. Consumers usually filter by `type` and check
    `status` ("True"/"False"/"Unknown")."""

    type: str
    status: str  # "True" / "False" / "Unknown"
    reason: str | None = None
    message: str | None = None


class K8sResource(BaseModel):
    """Parsed Kubernetes resource from YAML (generic base; see module docstring)."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    api_version: str = Field(default="", alias="apiVersion")
    metadata: K8sMetadata = Field(default_factory=K8sMetadata)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def namespace(self) -> str:
        return self.metadata.namespace


class PodContainer(CamelCaseModel):
    image: str | None = None


class ContainerDisk(CamelCaseModel):
    image: str | None = None


class ImageVolumeSource(BaseModel):
    reference: str


class PodVolume(CamelCaseModel):
    container_disk: ContainerDisk | None = None
    image: ImageVolumeSource | None = None


class PodSpec(CamelCaseModel):
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
        images.extend(volume.image.reference for volume in self.volumes if volume.image)
        return images


class PodTemplateSpec(CamelCaseModel):
    spec: PodSpec = Field(default_factory=PodSpec)


class PodTemplateWorkloadSpec(CamelCaseModel):
    template: PodTemplateSpec = Field(default_factory=PodTemplateSpec)


class PodTemplateWorkloadResource(K8sResource):
    spec: PodTemplateWorkloadSpec = Field(default_factory=PodTemplateWorkloadSpec)

    @property
    def images(self) -> list[str]:
        return self.spec.template.spec.images


class CronJobResource(PodTemplateWorkloadResource):
    spec: PodTemplateWorkloadSpec = Field(validation_alias=AliasPath("spec", "jobTemplate", "spec"))


class SandboxTemplateSpec(CamelCaseModel):
    pod_template: PodTemplateSpec = Field(default_factory=PodTemplateSpec)


class SandboxTemplateResource(K8sResource):
    spec: SandboxTemplateSpec = Field(default_factory=SandboxTemplateSpec)

    @property
    def images(self) -> list[str]:
        return self.spec.pod_template.spec.images


class FirecrackerTemplateSpec(CamelCaseModel):
    pod: PodTemplateSpec = Field(default_factory=PodTemplateSpec)
    boot_image: str | None = None
    runner_image: str | None = None


class AgentplaneSandboxTemplateSpec(BaseModel):
    pod: PodTemplateSpec | None = None
    vm: PodTemplateWorkloadSpec | None = None
    firecracker: FirecrackerTemplateSpec | None = None


class AgentplaneSandboxTemplateResource(K8sResource):
    spec: AgentplaneSandboxTemplateSpec = Field(default_factory=AgentplaneSandboxTemplateSpec)

    @property
    def images(self) -> list[str]:
        if self.spec.pod is not None:
            return self.spec.pod.spec.images
        if self.spec.vm is not None:
            return self.spec.vm.template.spec.images
        if firecracker := self.spec.firecracker:
            # The controller mounts these as OCI image volumes on the launcher Pod.
            volumes = [image for image in (firecracker.boot_image, firecracker.runner_image) if image]
            return [*firecracker.pod.spec.images, *volumes]
        return []


class ImageRepositoryResource(K8sResource):
    """An image-automation `ImageRepository`: `image_automation` matches them to namespaced Receiver references."""


class SecretRef(BaseModel):
    name: str = ""


class RbacRule(CamelCaseModel):
    model_config = ConfigDict(extra="forbid")

    api_groups: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    verbs: list[str]
    resource_names: list[str] = Field(default_factory=list)
    non_resource_urls: list[str] = Field(default_factory=list, alias="nonResourceURLs")


class RoleResource(K8sResource):
    rules: list[RbacRule] = Field(default_factory=list)


class AgentplanePolicyRule(BaseModel):
    type: str
    namespaces: list[str] = Field(default_factory=list)


class AgentplanePolicySetResource(K8sResource):
    """Delegation checks read namespaced role bindings from both Agentplane policy resource kinds."""

    rules: list[AgentplanePolicyRule] = Field(validation_alias=AliasPath("spec", "rules"))


class RbacRoleRef(CamelCaseModel):
    model_config = ConfigDict(extra="forbid")

    api_group: str
    kind: str
    name: str


class RoleBindingResource(K8sResource):
    role_ref: RbacRoleRef | None = Field(default=None, alias="roleRef")


class SecretStoreKubernetesProvider(CamelCaseModel):
    remote_namespace: str = ""
    auth: dict[str, Any] = Field(default_factory=dict)


class SecretStoreProvider(BaseModel):
    kubernetes: SecretStoreKubernetesProvider | None = None


class SecretStoreSpec(BaseModel):
    provider: SecretStoreProvider = Field(default_factory=SecretStoreProvider)
    conditions: list[dict[str, Any]] = Field(default_factory=list)


class SecretStoreResource(K8sResource):
    spec: SecretStoreSpec = Field(default_factory=SecretStoreSpec)


class ExternalSecretResource(K8sResource):
    """An External Secrets `ExternalSecret`: `checks` looks up the ones a namespace has by name."""

    spec: dict[str, Any] = Field(default_factory=dict)


class ClusterExternalSecretResource(K8sResource):
    spec: dict[str, Any] = Field(default_factory=dict)


class ImagePolicyResource(K8sResource):
    """An image-automation `ImagePolicy`: what a `$imagepolicy` setter marker names."""


_ArtifactCopy = TypedDict("_ArtifactCopy", {"from": str, "to": str})


class _ArtifactGeneratorArtifact(TypedDict):
    name: str
    copy: NotRequired[list[_ArtifactCopy]]


class ArtifactGeneratorSpec(BaseModel):
    artifacts: list[_ArtifactGeneratorArtifact] = Field(default_factory=list)


class ArtifactGeneratorResource(K8sResource):
    """source-watcher `ArtifactGenerator`: each `spec.artifacts[].name` is an `ExternalArtifact` the
    controller creates in the generator's namespace -- a Kustomization sourceRef target that never
    appears in Git itself."""

    spec: ArtifactGeneratorSpec = Field(default_factory=ArtifactGeneratorSpec)


class ReceiverResourceRef(BaseModel):
    kind: str = ""
    name: str = ""
    namespace: str | None = None


class ReceiverSpec(BaseModel):
    resources: list[ReceiverResourceRef] = []


class ReceiverResource(K8sResource):
    spec: ReceiverSpec = Field(default_factory=ReceiverSpec)


class SopsMetadata(BaseModel):
    """The `sops:` block on a SOPS-encrypted document. Only presence is read by
    validation: a rendered Secret carrying it is ciphertext Flux cannot apply
    without a `decryption` block. Inner fields (mac, age recipients, lastmodified)
    are ignored — model them only if a future check consumes them."""


class SecretResource(K8sResource):
    """A `Secret`. `sops` is set when the source document carries a SOPS metadata
    block, i.e. the rendered Secret is still ENC[...] ciphertext."""

    sops: SopsMetadata | None = None


class FluxControllerResource(K8sResource):
    """A HelmRelease or Terraform object whose readiness gates its Flux owner."""

    suspended: bool = Field(default=False, validation_alias=AliasPath("spec", "suspend"))


class CiliumPolicyResource(K8sResource):
    """A `CiliumNetworkPolicy` or `CiliumClusterwideNetworkPolicy`; its rules stay raw for
    `cluster.cdk8s.cilium_rule_sections`, which reads them as the fleet rules do."""

    spec: dict | None = None
    specs: list[dict] = Field(default_factory=list)

    @property
    def rules(self) -> list[dict]:
        return ([self.spec] if self.spec is not None else []) + self.specs


_KIND_MODELS: dict[str, type[K8sResource]] = {
    "ImageRepository": ImageRepositoryResource,
    "ImagePolicy": ImagePolicyResource,
    "ArtifactGenerator": ArtifactGeneratorResource,
    "Receiver": ReceiverResource,
    "Role": RoleResource,
    "ClusterRole": RoleResource,
    "RoleBinding": RoleBindingResource,
    "ClusterRoleBinding": RoleBindingResource,
    "Secret": SecretResource,
    "SecretStore": SecretStoreResource,
    "ClusterSecretStore": SecretStoreResource,
    "ClusterExternalSecret": ClusterExternalSecretResource,
    "ExternalSecret": ExternalSecretResource,
    "CiliumNetworkPolicy": CiliumPolicyResource,
    "CiliumClusterwideNetworkPolicy": CiliumPolicyResource,
    "CronJob": CronJobResource,
    "DaemonSet": PodTemplateWorkloadResource,
    "Deployment": PodTemplateWorkloadResource,
    "Job": PodTemplateWorkloadResource,
    "StatefulSet": PodTemplateWorkloadResource,
    "VirtualMachine": PodTemplateWorkloadResource,
}


_GROUP_KIND_MODELS: dict[tuple[str, str], type[K8sResource]] = {
    ("helm.toolkit.fluxcd.io", "HelmRelease"): FluxControllerResource,
    ("infra.contrib.fluxcd.io", "Terraform"): FluxControllerResource,
    ("agentplane.allegedly.works", "PolicySet"): AgentplanePolicySetResource,
    ("agentplane.allegedly.works", "KubernetesRoleSet"): AgentplanePolicySetResource,
    ("agentplane.allegedly.works", "SandboxTemplate"): AgentplaneSandboxTemplateResource,
    ("extensions.agents.x-k8s.io", "SandboxTemplate"): SandboxTemplateResource,
}


def _resource_model(doc: dict) -> type[K8sResource]:
    kind = doc["kind"]
    group = (doc.get("apiVersion") or "").rpartition("/")[0]
    return _GROUP_KIND_MODELS.get((group, kind), _KIND_MODELS.get(kind, K8sResource))


def parse_k8s_resources(docs: Iterable[object], *, kinds: set[str] | None = None) -> list[K8sResource]:
    """Build typed views of matching Kubernetes resources, skipping non-resource documents."""
    return [
        _resource_model(doc).model_validate(doc)
        for doc in docs
        if isinstance(doc, dict) and (kind := doc.get("kind")) and (kinds is None or kind in kinds)
    ]


def load_k8s_resources(data: bytes | str, *, kinds: set[str] | None = None) -> list[K8sResource]:
    return parse_k8s_resources(yaml.load_all(data, Loader=yaml.CSafeLoader), kinds=kinds)
