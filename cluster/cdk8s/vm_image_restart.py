"""The generic Kubernetes resources for a single-target KubeVirt image restart controller."""

from __future__ import annotations

import jsii
from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    Capability,
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from cluster.cdk8s import cilium, pod_policy
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo import images as forgejo_images
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine
from cluster.controllers.vm_image_restart.settings import Settings
from util.settings_contract import env_name

NAME = "vm-image-restart"
IMAGE = "git.allegedly.works/ducktape-ci/vm-image-restart:unset"
OPT_IN_ANNOTATION = "ducktape.org/auto-restart-template-changes"
STATE_CONFIGMAP_NAME = f"{NAME}-state"
_LABELS = {"app.kubernetes.io/name": NAME}


@jsii.implements(IApiResource)
class _NamedApiResource:
    """One namespaced custom resource, restricted to the configured object's name."""

    def __init__(self, *, api_group: str, resource_type: str, resource_name: str) -> None:
        self._api_group = api_group
        self._resource_type = resource_type
        self._resource_name = resource_name

    @property
    def api_group(self) -> str:
        return self._api_group

    @property
    def resource_type(self) -> str:
        return self._resource_type

    @property
    def resource_name(self) -> str:
        return self._resource_name


class VmImageRestartController(Construct):
    """Our policy: one generic controller manages one explicitly opted-in KubeVirt VM."""

    def __init__(self, scope: Construct, id: str, *, target_vm: VirtualMachine) -> None:
        super().__init__(scope, id)
        target_name = target_vm.metadata.name
        namespace = target_vm.metadata.namespace
        if not target_name or not namespace:
            raise ValueError("the configured target VM must have a name and namespace")

        service_account = ServiceAccount(
            self,
            "service-account",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=namespace,
                annotations={"description": "Identity for the generic KubeVirt VM image restart controller."},
            ),
        )
        state = ConfigMap(
            self,
            "state",
            metadata=ApiObjectMetadata(
                name=STATE_CONFIGMAP_NAME,
                namespace=namespace,
                annotations={"description": "Durable rollout state and bounded retry count for the configured VM."},
            ),
        )
        vm_resource = _NamedApiResource(
            api_group="kubevirt.io", resource_type="virtualmachines", resource_name=target_name
        )
        vmi_resource = _NamedApiResource(
            api_group="kubevirt.io", resource_type="virtualmachineinstances", resource_name=target_name
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=namespace,
                annotations={"description": "Narrow permissions for restarting and observing the configured VM."},
            ),
            rules=[
                RolePolicyRule(resources=[vm_resource], verbs=["get", "list", "watch"]),
                RolePolicyRule(resources=[vmi_resource], verbs=["get", "list", "watch", "delete"]),
                RolePolicyRule(resources=[state], verbs=["get", "patch"]),
                RolePolicyRule(resources=[custom_resource("", "events")], verbs=["create"]),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=namespace,
                labels=_LABELS,
                annotations={
                    "description": "Applies staged image/template changes to one configured stateless KubeVirt VM."
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            select=False,
            service_account=service_account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images.forgejo_images_creds_secret_ref(self, "image-pull-auth"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=65532, group=65532),
        )
        deployment.select(LabelSelector.of(labels=_LABELS))
        deployment.add_container(
            name=NAME,
            image=IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={
                env_name(Settings, "target_namespace"): EnvValue.from_value(namespace),
                env_name(Settings, "target_vm_name"): EnvValue.from_value(target_name),
                env_name(Settings, "state_configmap_name"): EnvValue.from_value(STATE_CONFIGMAP_NAME),
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(48), limit=Size.mebibytes(128)),
            ),
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                # aspect_py_binary's launcher creates its venv under runfiles at startup.
                read_only_root_filesystem=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
            ),
        )
        pod_policy.harden(deployment)
        NetworkPolicy(
            self,
            "egress",
            metadata=ApiObjectMetadata(
                name=f"{NAME}-egress",
                namespace=namespace,
                annotations={"description": "Allows the restart controller to reach only DNS and the Kubernetes API."},
            ),
            endpoint_selector=_LABELS,
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER, ports=[443, 6443])],
        )
