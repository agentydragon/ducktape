"""Agentplane's sandbox inventory: the labelled Sandboxes in one namespace and the Pod under each.

Kubernetes is the inventory in this slice — this component persists no private database — so every
fact it knows about a sandbox is a label or annotation on its Sandbox, and the provisioning state is
derived from the Sandbox and its Pod. It creates standalone Sandboxes: the Pod and volume
shape is copied from the namespace's `SandboxTemplate` at creation, so the manifest stays the one
place the runner Pod is defined, and no claim or warm pool sits in between.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import string
from typing import cast

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.policies.resources import CALLER_LABEL
from agentplane.egress.resources import placeholder_of
from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ERROR_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    MANAGED_LABEL,
    PROVISIONING_ANNOTATION,
    SANDBOX_BINDING_ANNOTATION,
    TEMPLATE_ANNOTATION,
    SandboxResource,
    sandbox_view,
    sandbox_views,
)
from agentplane.sandbox_service.kubevirt import (
    VmiResource,
    VmResource,
    VmTemplate,
    controller_owned_by,
    pod_owned_by_vmi,
    vm_view,
)
from agentplane.sandbox_service.kubevirt_contract import (
    KUBEVIRT_API_VERSION,
    LAUNCHER_SERVICE_ACCOUNT_ANNOTATION,
    VM_DESIRED_MODE_ANNOTATION,
    VM_KIND,
    VM_STATE_SCHEMA_ANNOTATION,
    VM_TEMPLATE_ANNOTATION,
    VM_TEMPLATE_CONFIG_ANNOTATION,
    VMIS_PLURAL,
    VMS_PLURAL,
    vm_aux_name,
)
from agentplane.sandbox_service.models import (
    EnvironmentKind,
    InventoryError,
    OperatingMode,
    SandboxNotFoundError,
    SandboxRunningError,
    environment_kind,
)
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Sandbox, SandboxBinding, TemplateDescriptor
from agentplane.sandbox_service.session_config import LaunchGrants
from util.agent_sandbox import EXTENSIONS_API, SANDBOX_API, SANDBOXES_PLURAL, TEMPLATES_PLURAL
from util.kubernetes import CustomObjectsClient

_MERGE_PATCH = "application/merge-patch+json"

# Five lowercase alphanumerics, like `generateName`; the slug bound keeps the name a DNS label.
_SUFFIX_LENGTH = 5
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


# Kubernetes-boundary models: the subset of each CR the inventory reads, parsed once off the wire.


class _TemplateSpec(BaseModel):
    """The parts of a SandboxTemplate a standalone Sandbox carries verbatim."""

    model_config = ConfigDict(extra="ignore")

    pod_template: dict[str, object] = Field(alias="podTemplate")
    volume_claim_templates: list[dict[str, object]] = Field(alias="volumeClaimTemplates", default_factory=list)


class _Template(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spec: _TemplateSpec


class _NamedMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _NamedResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _NamedMetadata


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, object]]


class SandboxInventory:
    def __init__(
        self,
        *,
        namespace: str,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        vm_templates: dict[str, VmTemplate] | None = None,
    ):
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._core_v1 = core_v1
        self._vm_templates = vm_templates or {}

    @property
    def namespace(self) -> str:
        return self._namespace

    async def list_templates(self) -> list[str]:
        """The concrete templates an operator may choose for one Sandbox."""
        page = await self._custom_objects.list_namespaced_custom_object(
            *EXTENSIONS_API, self._namespace, TEMPLATES_PLURAL
        )
        return sorted(
            _NamedResource.model_validate(item).metadata.name for item in _ResourceList.model_validate(page).items
        )

    async def list_template_descriptors(self) -> list[TemplateDescriptor]:
        containers = [
            TemplateDescriptor(name=name, kind=EnvironmentKind.AGENT_SANDBOX, capabilities=["pod_exec", "stop_start"])
            for name in await self.list_templates()
        ]
        vms = [
            TemplateDescriptor(name=name, kind=EnvironmentKind.KUBEVIRT, capabilities=["stop_start"])
            for name in sorted(self._vm_templates)
        ]
        return [*containers, *vms]

    async def list_sandboxes(self) -> list[Sandbox]:
        sandboxes_page, vms_page, vmis_page, pods = await asyncio.gather(
            self._custom_objects.list_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, label_selector=f"{MANAGED_LABEL}=true"
            ),
            self._custom_objects.list_namespaced_custom_object(
                "kubevirt.io", "v1", self._namespace, VMS_PLURAL, label_selector=f"{MANAGED_LABEL}=true"
            ),
            self._custom_objects.list_namespaced_custom_object("kubevirt.io", "v1", self._namespace, VMIS_PLURAL),
            self._core_v1.list_namespaced_pod(self._namespace),
        )
        containers = sandbox_views(_ResourceList.model_validate(sandboxes_page).items, pods.items)
        vmis = [VmiResource.model_validate(item) for item in _ResourceList.model_validate(vmis_page).items]
        virtual_machines = []
        for raw in _ResourceList.model_validate(vms_page).items:
            vm = VmResource.model_validate(raw)
            owned = [
                vmi
                for vmi in vmis
                if controller_owned_by(
                    vmi.metadata,
                    api_version=KUBEVIRT_API_VERSION,
                    kind=VM_KIND,
                    name=vm.metadata.name,
                    uid=vm.metadata.uid,
                )
            ]
            vmi = owned[0] if len(owned) == 1 else None
            matching = [pod for pod in pods.items if vmi is not None and pod_owned_by_vmi(pod, vmi)]
            virtual_machines.append(vm_view(vm, vmi, matching[0] if len(matching) == 1 else None))
        return [*containers, *virtual_machines]

    async def get(self, name: str, *, kind: str = "") -> Sandbox:
        if environment_kind(kind) == EnvironmentKind.KUBEVIRT:
            vm = await self._vm(name)
            try:
                raw_vmi = await self._custom_objects.get_namespaced_custom_object(
                    "kubevirt.io", "v1", self._namespace, VMIS_PLURAL, name
                )
            except k8s_client.ApiException as error:
                if error.status != 404:
                    raise
                vmi = None
            else:
                vmi = VmiResource.model_validate(raw_vmi)
            pods = await self._core_v1.list_namespaced_pod(self._namespace)
            matching = [pod for pod in pods.items if vmi is not None and pod_owned_by_vmi(pod, vmi)]
            return vm_view(vm, vmi, matching[0] if len(matching) == 1 else None)
        sandbox = await self._sandbox(name)
        return sandbox_view(sandbox, await self._pod(name))

    async def create(
        self,
        spec: CreateSandboxRequest,
        *,
        annotations: dict[str, str] | None = None,
        finalizers: list[str] | None = None,
    ) -> Sandbox:
        if environment_kind(spec.kind) == EnvironmentKind.KUBEVIRT:
            return await self._create_vm(spec, annotations=annotations, finalizers=finalizers)
        template = _Template.model_validate(
            await self._custom_objects.get_namespaced_custom_object(
                *EXTENSIONS_API, self._namespace, TEMPLATES_PLURAL, spec.template
            )
        )
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
        name = f"{spec.slug}-{suffix}"
        # Before the Sandbox, because its Pod names this ServiceAccount: a Pod whose ServiceAccount
        # does not exist is refused admission, and the token projected for the proxy's audience is
        # minted for it. The owner reference cannot be set yet -- the Sandbox has no UID until it is
        # created -- so it is patched on directly afterwards and the account is deleted if the
        # Sandbox never appears, rather than being left for nothing to collect.
        await self._core_v1.create_namespaced_service_account(
            self._namespace,
            k8s_client.V1ServiceAccount(
                metadata=k8s_client.V1ObjectMeta(
                    # The Action Service admits an account only while it carries its caller label,
                    # so a sandbox without this one authenticates and reaches no route.
                    name=name,
                    labels={MANAGED_LABEL: "true", CALLER_LABEL: "true"},
                )
            ),
        )
        body = {
            "apiVersion": SANDBOX_API.api_version,
            "kind": "Sandbox",
            "metadata": {
                "name": name,
                "labels": {MANAGED_LABEL: "true"},
                "annotations": {**(annotations or {}), TEMPLATE_ANNOTATION: spec.template},
                **({"finalizers": finalizers} if finalizers else {}),
            },
            # No shutdownTime and Retain: the app owns deletion, nothing expires a sandbox behind it.
            "spec": {
                "podTemplate": _running_as(template.spec.pod_template, name),
                "volumeClaimTemplates": template.spec.volume_claim_templates,
                "shutdownPolicy": "Retain",
            },
        }
        try:
            created = await self._custom_objects.create_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, body
            )
        except Exception:
            await self._core_v1.delete_namespaced_service_account(name, self._namespace)
            raise
        sandbox = SandboxResource.model_validate(created)
        await self._core_v1.patch_namespaced_service_account(
            name,
            self._namespace,
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": SANDBOX_API.api_version,
                            "kind": "Sandbox",
                            "name": name,
                            "uid": str(sandbox.metadata.uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ]
                }
            },
        )
        return sandbox_view(sandbox, None)

    async def _create_vm(
        self, spec: CreateSandboxRequest, *, annotations: dict[str, str] | None, finalizers: list[str] | None
    ) -> Sandbox:
        template = self._vm_templates.get(spec.template)
        if template is None:
            raise ValueError(f"unknown approved KubeVirt template {spec.template!r}")
        name = f"{spec.slug}-{''.join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))}"
        account_name = vm_aux_name(name, "account")
        await self._core_v1.create_namespaced_service_account(
            self._namespace,
            k8s_client.V1ServiceAccount(
                metadata=k8s_client.V1ObjectMeta(
                    name=account_name, labels={MANAGED_LABEL: "true", CALLER_LABEL: "true"}
                ),
                image_pull_secrets=[k8s_client.V1LocalObjectReference(name=template.image_pull_secret)],
            ),
        )
        disks = [
            {"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1},
            {"name": "state", "disk": {"bus": "virtio"}, "serial": "state"},
            {"name": "workspace", "disk": {"bus": "virtio"}, "serial": "workspace"},
            {"name": "config", "disk": {"bus": "virtio"}, "serial": "agentplane-config"},
        ]
        body = {
            "apiVersion": KUBEVIRT_API_VERSION,
            "kind": VM_KIND,
            "metadata": {
                "name": name,
                "labels": {MANAGED_LABEL: "true"},
                "annotations": {
                    **(annotations or {}),
                    LAUNCHER_SERVICE_ACCOUNT_ANNOTATION: account_name,
                    VM_TEMPLATE_ANNOTATION: spec.template,
                    VM_TEMPLATE_CONFIG_ANNOTATION: template.model_dump_json(),
                    VM_STATE_SCHEMA_ANNOTATION: str(template.state_schema_version),
                    VM_DESIRED_MODE_ANNOTATION: OperatingMode.RUNNING,
                },
                **({"finalizers": finalizers} if finalizers else {}),
            },
            "spec": {
                "runStrategy": "Halted",
                "template": {
                    "metadata": {"labels": {MANAGED_LABEL: "true"}},
                    "spec": {
                        "evictionStrategy": "None",
                        # KubeVirt checks the guest listener, so launcher Pod readiness alone
                        # cannot report a booting OS as a usable runner.
                        "readinessProbe": {
                            "tcpSocket": {"port": 7000},
                            "initialDelaySeconds": 10,
                            "periodSeconds": 5,
                            "timeoutSeconds": 2,
                        },
                        "nodeSelector": template.node_selector,
                        "domain": {
                            "cpu": {"cores": template.cpu_cores},
                            "resources": {"requests": {"memory": template.memory}},
                            "firmware": {"bootloader": {"efi": {"secureBoot": False}}},
                            "devices": {
                                "disks": disks,
                                "interfaces": [
                                    {
                                        "name": "default",
                                        "masquerade": {},
                                        "ports": [{"name": "runner", "port": 7000, "protocol": "TCP"}],
                                    }
                                ],
                                "autoattachGraphicsDevice": False,
                            },
                        },
                        "networks": [{"name": "default", "pod": {}}],
                        "volumes": [
                            {
                                "name": "root",
                                "containerDisk": {
                                    "image": template.image,
                                    "imagePullSecret": template.image_pull_secret,
                                },
                            },
                            {"name": "state", "persistentVolumeClaim": {"claimName": vm_aux_name(name, "state")}},
                            {
                                "name": "workspace",
                                "persistentVolumeClaim": {"claimName": vm_aux_name(name, "workspace")},
                            },
                            {"name": "config", "configMap": {"name": vm_aux_name(name, "config")}},
                        ],
                    },
                },
            },
        }
        try:
            created = await self._custom_objects.create_namespaced_custom_object(
                "kubevirt.io", "v1", self._namespace, VMS_PLURAL, body
            )
        except Exception as error:
            # A timeout may follow a committed create. Keep its ServiceAccount for reconciliation.
            if isinstance(error, k8s_client.ApiException) and error.status == 409:
                await self._core_v1.delete_namespaced_service_account(account_name, self._namespace)
                raise
            try:
                existing = await self._vm(name)
            except SandboxNotFoundError:
                await self._core_v1.delete_namespaced_service_account(account_name, self._namespace)
            else:
                if existing.metadata.annotations.get(LAUNCHER_SERVICE_ACCOUNT_ANNOTATION) != account_name:
                    raise ValueError("uncertain VM create collided with a different resource") from None
            raise
        vm = VmResource.model_validate(created)
        await self._core_v1.patch_namespaced_service_account(
            account_name,
            self._namespace,
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": KUBEVIRT_API_VERSION,
                            "kind": VM_KIND,
                            "name": name,
                            "uid": vm.metadata.uid,
                            "controller": True,
                            "blockOwnerDeletion": False,
                        }
                    ]
                }
            },
        )
        return vm_view(vm, None, None)

    async def ensure_vm_dependencies(self, sandbox: Sandbox) -> bool:
        if environment_kind(sandbox.kind) != EnvironmentKind.KUBEVIRT:
            return True
        vm = await self._vm(sandbox.name)
        template = VmTemplate.model_validate_json(vm.metadata.annotations[VM_TEMPLATE_CONFIG_ANNOTATION])
        await self._ensure_vm_service_account(vm, template)
        for disk_name, size in (("state", template.state_disk), ("workspace", template.workspace_disk)):
            await self._ensure_data_volume(vm, disk_name, size, template.storage_class)
        await self._ensure_vm_config(vm, template)
        return await self._vm_disks_ready(vm)

    async def _ensure_vm_service_account(self, vm: VmResource, template: VmTemplate) -> None:
        name = vm.metadata.annotations.get(LAUNCHER_SERVICE_ACCOUNT_ANNOTATION)
        if name != vm_aux_name(vm.metadata.name, "account"):
            raise ValueError("VM launcher ServiceAccount does not match its environment")
        try:
            account = await self._core_v1.read_namespaced_service_account(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            try:
                account = await self._core_v1.create_namespaced_service_account(
                    self._namespace,
                    k8s_client.V1ServiceAccount(
                        metadata=k8s_client.V1ObjectMeta(
                            name=name, labels={MANAGED_LABEL: "true", CALLER_LABEL: "true"}
                        ),
                        image_pull_secrets=[k8s_client.V1LocalObjectReference(name=template.image_pull_secret)],
                    ),
                )
            except k8s_client.ApiException as create_error:
                if create_error.status != 409:
                    raise
                account = await self._core_v1.read_namespaced_service_account(name, self._namespace)
        if (
            account.metadata is None
            or account.metadata.labels is None
            or (
                account.metadata.labels.get(MANAGED_LABEL) != "true"
                or account.metadata.labels.get(CALLER_LABEL) != "true"
                or [ref.name for ref in account.image_pull_secrets or []] != [template.image_pull_secret]
            )
        ):
            raise ValueError("VM ServiceAccount has conflicting labels")
        owners = account.metadata.owner_references or []
        if owners:
            if (
                len(owners) != 1
                or not owners[0].controller
                or owners[0].uid != vm.metadata.uid
                or (owners[0].api_version != KUBEVIRT_API_VERSION or owners[0].kind != VM_KIND)
            ):
                raise ValueError("VM ServiceAccount belongs to another resource")
            return
        await self._core_v1.patch_namespaced_service_account(
            name,
            self._namespace,
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": KUBEVIRT_API_VERSION,
                            "kind": VM_KIND,
                            "name": vm.metadata.name,
                            "uid": vm.metadata.uid,
                            "controller": True,
                            "blockOwnerDeletion": False,
                        }
                    ]
                }
            },
        )

    async def _ensure_data_volume(self, vm: VmResource, disk_name: str, size: str, storage_class: str) -> None:
        name = vm_aux_name(vm.metadata.name, disk_name)
        body = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": name,
                "labels": {MANAGED_LABEL: "true"},
                "annotations": {"agentplane.allegedly.works/environment-uid": vm.metadata.uid},
            },
            "spec": {
                "source": {"blank": {}},
                "pvc": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": storage_class,
                    "resources": {"requests": {"storage": size}},
                },
            },
        }
        try:
            actual = await self._custom_objects.get_namespaced_custom_object(
                "cdi.kubevirt.io", "v1beta1", self._namespace, "datavolumes", name
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            try:
                actual = await self._custom_objects.create_namespaced_custom_object(
                    "cdi.kubevirt.io", "v1beta1", self._namespace, "datavolumes", body
                )
            except k8s_client.ApiException as create_error:
                if create_error.status != 409:
                    raise
                actual = await self._custom_objects.get_namespaced_custom_object(
                    "cdi.kubevirt.io", "v1beta1", self._namespace, "datavolumes", name
                )
        if (
            actual.get("metadata", {}).get("annotations", {}).get("agentplane.allegedly.works/environment-uid")
            != vm.metadata.uid
        ):
            raise ValueError(f"DataVolume {name} belongs to another environment")
        actual_spec = actual.get("spec", {})
        actual_pvc = actual_spec.get("pvc", {})
        if (
            actual_spec.get("source") != {"blank": {}}
            or actual_pvc.get("accessModes") != ["ReadWriteOnce"]
            or actual_pvc.get("storageClassName") != storage_class
            or actual_pvc.get("resources", {}).get("requests", {}).get("storage") != size
        ):
            raise ValueError(f"DataVolume {name} does not match its approved template")

    async def _ensure_vm_config(self, vm: VmResource, template: VmTemplate) -> None:
        name = vm_aux_name(vm.metadata.name, "config")
        config = {
            "version": 1,
            "environment_id": vm.metadata.uid,
            "listen": "0.0.0.0:7000",
            "llm_base_url": template.llm_base_url,
            "proxy_url": template.proxy_url,
            "model_context_windows": template.model_context_windows,
            "format_blank_disks": ["state", "workspace"],
        }
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "in-cluster",
                    "cluster": {
                        "server": f"https://{template.kubernetes_host}",
                        "certificate-authority": "/run/agentplane/ca-certificates.crt",
                    },
                }
            ],
            "users": [{"name": "workload", "user": {"token": placeholder_of(template.kubernetes_credential_name)}}],
            "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "workload"}}],
            "current-context": "in-cluster",
        }
        expected = {
            "config.json": json.dumps(config, separators=(",", ":")),
            "ca-certificates.crt": template.ca_bundle,
            "kubeconfig": json.dumps(kubeconfig, separators=(",", ":")),
        }
        try:
            actual = await self._core_v1.read_namespaced_config_map(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            try:
                await self._core_v1.create_namespaced_config_map(
                    self._namespace,
                    k8s_client.V1ConfigMap(
                        metadata=k8s_client.V1ObjectMeta(
                            name=name,
                            owner_references=[
                                k8s_client.V1OwnerReference(
                                    api_version=KUBEVIRT_API_VERSION,
                                    kind=VM_KIND,
                                    name=vm.metadata.name,
                                    uid=vm.metadata.uid,
                                    controller=False,
                                    block_owner_deletion=False,
                                )
                            ],
                        ),
                        data=expected,
                    ),
                )
            except k8s_client.ApiException as create_error:
                if create_error.status != 409:
                    raise
                actual = await self._core_v1.read_namespaced_config_map(name, self._namespace)
            else:
                return
        held = dict(actual.data or {})
        held_config = json.loads(held.get("config.json", "{}"))
        if held_config.get("format_blank_disks") in ([], ["state", "workspace"]):
            held_config["format_blank_disks"] = ["state", "workspace"]
            held["config.json"] = json.dumps(held_config, separators=(",", ":"))
        if (
            held != expected
            or actual.metadata is None
            or not actual.metadata.owner_references
            or (
                len(actual.metadata.owner_references) != 1 or actual.metadata.owner_references[0].uid != vm.metadata.uid
            )
        ):
            raise ValueError(f"ConfigMap {name} does not belong to this VM")

    async def retire_vm_disk_initialization(self, name: str, uid: str) -> None:
        vm = await self._vm(name)
        if vm.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        config_name = vm_aux_name(name, "config")
        config_map = await self._core_v1.read_namespaced_config_map(config_name, self._namespace)
        if (
            config_map.metadata is None
            or len(config_map.metadata.owner_references or []) != 1
            or (config_map.metadata.owner_references[0].uid != uid)
        ):
            raise ValueError("VM config disk belongs to another resource")
        data = dict(config_map.data or {})
        config = json.loads(data["config.json"])
        if config.get("environment_id") != uid:
            raise ValueError("VM config disk has a different environment identity")
        if config.get("format_blank_disks") == []:
            return
        if config.get("format_blank_disks") != ["state", "workspace"]:
            raise ValueError("VM config disk has an unknown initialization authorization")
        config["format_blank_disks"] = []
        await self._core_v1.patch_namespaced_config_map(
            config_name,
            self._namespace,
            {
                "metadata": {"resourceVersion": config_map.metadata.resource_version},
                "data": {"config.json": json.dumps(config, separators=(",", ":"))},
            },
        )

    async def _vm_disks_ready(self, vm: VmResource) -> bool:
        for disk_name in ("state", "workspace"):
            name = vm_aux_name(vm.metadata.name, disk_name)
            volume = await self._custom_objects.get_namespaced_custom_object(
                "cdi.kubevirt.io", "v1beta1", self._namespace, "datavolumes", name
            )
            # The local class can wait for a VMI consumer before the PVC binds. The VM
            # controller waits for usable disks before starting the guest.
            if volume.get("status", {}).get("phase") in {"Failed", "Unknown"}:
                raise ValueError(f"DataVolume {name} failed to initialize")
        return True

    async def pending_grants(self, name: str, *, kind: str = "") -> LaunchGrants | None:
        resource = await self._resource(name, kind)
        raw = resource.metadata.annotations.get(PROVISIONING_ANNOTATION)
        return LaunchGrants.model_validate_json(raw) if raw is not None else None

    async def finish_provisioning(self, sandbox: Sandbox) -> None:
        vm = await self._vm(sandbox.name) if environment_kind(sandbox.kind) == EnvironmentKind.KUBEVIRT else None
        if vm is not None and vm.metadata.uid != sandbox.uid:
            raise SandboxNotFoundError(sandbox.name)
        await self._patch(
            sandbox.name,
            {
                "metadata": {
                    "uid": str(sandbox.uid),
                    **({"resourceVersion": vm.metadata.resource_version} if vm is not None else {}),
                    "annotations": {PROVISIONING_ANNOTATION: None},
                },
                # Keep the durable provisioning intent until starting the VM also succeeds.
                # Separate patches can lose the retry intent when a controller status update
                # conflicts with the runStrategy change.
                **(
                    {"spec": {"runStrategy": "Always"}}
                    if vm is not None
                    and vm.metadata.annotations.get(VM_DESIRED_MODE_ANNOTATION) == OperatingMode.RUNNING
                    else {}
                ),
            },
            kind=sandbox.kind,
        )

    async def binding(self, name: str, *, kind: str = "") -> SandboxBinding | None:
        raw = (await self._resource(name, kind)).metadata.annotations.get(SANDBOX_BINDING_ANNOTATION)
        if raw is None:
            return None
        return read_binding(raw)

    async def set_kubernetes_grants_status(
        self, name: str, *, ready: bool, error: str | None = None, kind: str = ""
    ) -> None:
        """Record provisioning so a runner cannot start before requested bindings exist."""
        await self._patch(
            name,
            {
                "metadata": {
                    "annotations": {
                        KUBERNETES_GRANTS_READY_ANNOTATION: "true" if ready else "false",
                        KUBERNETES_GRANTS_ERROR_ANNOTATION: error,
                    }
                }
            },
            kind=kind,
        )

    async def remove_finalizer(self, name: str, finalizer: str, *, kind: str = "") -> None:
        sandbox = await self._resource(name, kind)
        if finalizer in sandbox.metadata.finalizers:
            await self._patch(
                name,
                {"metadata": {"finalizers": [item for item in sandbox.metadata.finalizers if item != finalizer]}},
                kind=kind,
            )

    async def suspend(self, name: str, *, uid: str | None = None, kind: str = "") -> None:
        await self._set_operating_mode(name, OperatingMode.SUSPENDED, uid=uid, kind=kind)

    async def resume(self, name: str, *, uid: str | None = None, kind: str = "") -> None:
        await self._set_operating_mode(name, OperatingMode.RUNNING, uid=uid, kind=kind)

    async def delete(self, name: str, *, uid: str | None = None, kind: str = "") -> None:
        """Delete a suspended Sandbox; the controller removes its Pod and PVC, and with them
        everything on the volume. A running one is refused, so the irreversible step is a
        deliberate second one for a browser and for an agent calling the API alike."""
        sandbox = await self._resource(name, kind)
        if uid is not None and sandbox.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        if (
            sandbox.spec.get("runStrategy") != "Halted"
            if isinstance(sandbox, VmResource)
            else sandbox.spec.operating_mode != OperatingMode.SUSPENDED
        ):
            raise SandboxRunningError(name)
        if isinstance(sandbox, VmResource):
            try:
                await self._custom_objects.get_namespaced_custom_object(
                    "kubevirt.io", "v1", self._namespace, VMIS_PLURAL, name
                )
            except k8s_client.ApiException as error:
                if error.status != 404:
                    raise
            else:
                raise SandboxRunningError(name)
        await self._custom_objects.delete_namespaced_custom_object(
            *(("kubevirt.io", "v1") if isinstance(sandbox, VmResource) else SANDBOX_API),
            self._namespace,
            VMS_PLURAL if isinstance(sandbox, VmResource) else SANDBOXES_PLURAL,
            name,
            body=k8s_client.V1DeleteOptions(preconditions=k8s_client.V1Preconditions(uid=str(sandbox.metadata.uid))),
        )

    async def replace_vm_image(self, name: str, *, uid: str, template_name: str) -> Sandbox:
        vm = await self._vm(name)
        if vm.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        if vm.spec.get("runStrategy") != "Halted":
            raise SandboxRunningError(name)
        try:
            await self._custom_objects.get_namespaced_custom_object(
                "kubevirt.io", "v1", self._namespace, VMIS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
        else:
            raise SandboxRunningError(name)
        if vm.metadata.annotations.get(VM_TEMPLATE_ANNOTATION) != template_name:
            raise ValueError("VM image replacement must use its selected template")
        template = self._vm_templates.get(template_name)
        if template is None:
            raise ValueError("approved VM template is unavailable")
        if vm.metadata.annotations.get(VM_STATE_SCHEMA_ANNOTATION) != str(template.state_schema_version):
            raise ValueError("approved image does not declare the VM's retained state schema")
        previous = VmTemplate.model_validate_json(vm.metadata.annotations[VM_TEMPLATE_CONFIG_ANNOTATION])
        if previous.model_copy(update={"image": template.image}).model_dump() != template.model_dump():
            raise ValueError("image replacement may change only the approved image digest")
        volumes = vm.spec.get("template", {}).get("spec", {}).get("volumes", [])
        if not isinstance(volumes, list) or len([item for item in volumes if item.get("name") == "root"]) != 1:
            raise ValueError("VM root disk is not the approved shape")
        replacement = [
            {**item, "containerDisk": {"image": template.image, "imagePullSecret": template.image_pull_secret}}
            if item.get("name") == "root"
            else item
            for item in volumes
        ]
        # resourceVersion rejects a concurrent resume or spec edit between the stopped check and patch.
        await self._patch(
            name,
            {
                "metadata": {
                    "uid": uid,
                    "resourceVersion": vm.metadata.resource_version,
                    "annotations": {VM_TEMPLATE_CONFIG_ANNOTATION: template.model_dump_json()},
                },
                "spec": {"template": {"spec": {"volumes": replacement}}},
            },
            kind=EnvironmentKind.KUBEVIRT,
        )
        return await self.get(name, kind=EnvironmentKind.KUBEVIRT)

    async def _set_operating_mode(
        self, name: str, mode: OperatingMode, *, uid: str | None = None, kind: str = ""
    ) -> None:
        sandbox = await self._resource(name, kind)
        if uid is not None and sandbox.metadata.uid != uid:
            raise SandboxNotFoundError(name)
        if isinstance(sandbox, VmResource):
            if mode == OperatingMode.RUNNING and PROVISIONING_ANNOTATION in sandbox.metadata.annotations:
                raise ValueError("VM grants are not ready")
            if mode == OperatingMode.RUNNING and sandbox.spec.get("runStrategy") == "Halted":
                try:
                    await self._custom_objects.get_namespaced_custom_object(
                        "kubevirt.io", "v1", self._namespace, VMIS_PLURAL, name
                    )
                except k8s_client.ApiException as error:
                    if error.status != 404:
                        raise
                else:
                    raise InventoryError("the previous VM instance is still stopping")
            patch = {
                "metadata": {
                    "uid": sandbox.metadata.uid,
                    "resourceVersion": sandbox.metadata.resource_version,
                    "annotations": {VM_DESIRED_MODE_ANNOTATION: mode},
                },
                "spec": {"runStrategy": "Always" if mode == OperatingMode.RUNNING else "Halted"},
            }
        else:
            patch = {"metadata": {"uid": str(sandbox.metadata.uid)}, "spec": {"operatingMode": mode}}
        await self._patch(name, patch, kind=kind)

    async def _patch(self, name: str, patch: dict[str, object], *, kind: str = "") -> None:
        await self._custom_objects.patch_namespaced_custom_object(
            *(("kubevirt.io", "v1") if environment_kind(kind) == EnvironmentKind.KUBEVIRT else SANDBOX_API),
            self._namespace,
            VMS_PLURAL if environment_kind(kind) == EnvironmentKind.KUBEVIRT else SANDBOXES_PLURAL,
            name,
            patch,
            _content_type=_MERGE_PATCH,
        )

    async def _resource(self, name: str, kind: str) -> SandboxResource | VmResource:
        return await self._vm(name) if environment_kind(kind) == EnvironmentKind.KUBEVIRT else await self._sandbox(name)

    async def _vm(self, name: str) -> VmResource:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                "kubevirt.io", "v1", self._namespace, VMS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        vm = VmResource.model_validate(raw)
        if vm.metadata.labels.get(MANAGED_LABEL) != "true":
            raise SandboxNotFoundError(name)
        return vm

    async def current_vm_instance(self, name: str, uid: str) -> VmiResource | None:
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                "kubevirt.io", "v1", self._namespace, VMIS_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise
        vmi = VmiResource.model_validate(raw)
        if (
            not controller_owned_by(vmi.metadata, api_version=KUBEVIRT_API_VERSION, kind=VM_KIND, name=name, uid=uid)
            or vmi.metadata.deletion_timestamp is not None
        ):
            return None
        return vmi

    async def _sandbox(self, name: str) -> SandboxResource:
        """The named Sandbox, only if it is Agentplane's: an unmanaged one is not in this inventory."""
        try:
            raw = await self._custom_objects.get_namespaced_custom_object(
                *SANDBOX_API, self._namespace, SANDBOXES_PLURAL, name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxNotFoundError(name) from error
            raise
        sandbox = SandboxResource.model_validate(raw)
        if sandbox.metadata.labels.get(MANAGED_LABEL) != "true":
            raise SandboxNotFoundError(name)
        return sandbox

    async def _pod(self, name: str) -> k8s_client.V1Pod | None:
        try:
            return await self._core_v1.read_namespaced_pod(name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return None
            raise


# The projection, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed row and a fetched one are the
# same row.


def _running_as(pod_template: dict[str, object], service_account: str) -> dict[str, object]:
    """The template's Pod, running as this sandbox's own ServiceAccount rather than the shared one.

    What a Pod runs as is what the egress proxy and the Action Service authenticate it by, so an
    account of its own is what lets a sandbox be granted something its neighbours are not. The
    template still owns every other field, including whether a token is automounted.
    """
    spec = {**cast(dict[str, object], pod_template.get("spec", {})), "serviceAccountName": service_account}
    return {**pod_template, "spec": spec}


# gazelle:include_dep @pypi//protobuf
