"""KubeVirt inventory and observations for managed execution environments."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from google.protobuf.json_format import ParseDict
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel, ConfigDict, Field

from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ANNOTATION,
    KUBERNETES_GRANTS_ERROR_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    PROVISIONING_ANNOTATION,
    SANDBOX_BINDING_ANNOTATION,
    _pod_status,
)
from agentplane.sandbox_service.kubevirt_contract import (
    KUBEVIRT_API_VERSION,
    LAUNCHER_SERVICE_ACCOUNT_ANNOTATION,
    VM_DESIRED_MODE_ANNOTATION,
    VM_KIND,
    VM_TEMPLATE_ANNOTATION,
    VMI_KIND,
)
from agentplane.sandbox_service.models import EnvironmentKind, ProvisioningState
from agentplane.sandbox_service.protocol_pb2 import (
    Condition,
    ResolvedGrant,
    Sandbox,
    ServiceAccount,
    VirtualMachineStatus,
)


class VmTemplate(BaseModel):
    """Operator reviewed VM shape; an empty catalog makes VM creation unavailable."""

    model_config = ConfigDict(extra="forbid")

    image: str = Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")
    image_pull_secret: str = Field(min_length=1)
    state_schema_version: int = Field(default=1, ge=1)
    cpu_cores: int = Field(default=4, ge=4)
    memory: str = Field(default="10Gi", pattern=r"^(?:8|9|[1-9][0-9]+)Gi$")
    state_disk: str = "20Gi"
    workspace_disk: str = "40Gi"
    storage_class: str = Field(min_length=1)
    node_selector: dict[str, str] = Field(default_factory=dict)
    llm_base_url: str = Field(min_length=1)
    proxy_url: str = Field(min_length=1)
    model_context_windows: dict[str, Annotated[int, Field(gt=0)]] = Field(default_factory=dict)
    ca_bundle: str = Field(min_length=1)
    kubernetes_host: str = Field(min_length=1)
    kubernetes_credential_name: str = Field(min_length=1)


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str
    uid: str
    resource_version: str = Field(alias="resourceVersion", default="")
    creation_timestamp: datetime = Field(alias="creationTimestamp")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    finalizers: list[str] = Field(default_factory=list)
    deletion_timestamp: datetime | None = Field(alias="deletionTimestamp", default=None)
    owner_references: list[dict[str, Any]] = Field(alias="ownerReferences", default_factory=list)


class VmResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _Metadata
    spec: dict[str, Any]
    status: dict[str, Any] = Field(default_factory=dict)


class VmiResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _Metadata
    status: dict[str, Any] = Field(default_factory=dict)


def controller_owned_by(resource: _Metadata, *, api_version: str, kind: str, name: str, uid: str) -> bool:
    controllers = [reference for reference in resource.owner_references if reference.get("controller") is True]
    return len(controllers) == 1 and all(
        controllers[0].get(key) == value
        for key, value in (("apiVersion", api_version), ("kind", kind), ("name", name), ("uid", uid))
    )


def pod_owned_by_vmi(pod: k8s_client.V1Pod, vmi: VmiResource) -> bool:
    meta = pod.metadata
    if meta is None or meta.namespace != vmi.metadata.namespace or meta.deletion_timestamp is not None:
        return False
    controllers = [reference for reference in meta.owner_references or [] if reference.controller]
    return len(controllers) == 1 and all(
        getattr(controllers[0], attr) == value
        for attr, value in (
            ("api_version", KUBEVIRT_API_VERSION),
            ("kind", VMI_KIND),
            ("name", vmi.metadata.name),
            ("uid", vmi.metadata.uid),
        )
    )


def vm_view(vm: VmResource, vmi: VmiResource | None, pod: k8s_client.V1Pod | None) -> Sandbox:
    meta = vm.metadata
    if vmi is not None and not controller_owned_by(
        vmi.metadata, api_version=KUBEVIRT_API_VERSION, kind=VM_KIND, name=meta.name, uid=meta.uid
    ):
        vmi = None
    if pod is not None and (vmi is None or not pod_owned_by_vmi(pod, vmi)):
        pod = None
    created_at = Timestamp()
    created_at.FromDatetime(meta.creation_timestamp)
    annotations = meta.annotations
    grants = [
        ParseDict(item, ResolvedGrant()) for item in json.loads(annotations.get(KUBERNETES_GRANTS_ANNOTATION, "[]"))
    ]
    conditions = [_condition(item) for item in vm.status.get("conditions", [])]
    vmi_status = vmi.status if vmi is not None else {}
    vmi_conditions = [_condition(item) for item in vmi_status.get("conditions", [])]
    mode = annotations.get(
        VM_DESIRED_MODE_ANNOTATION, "Suspended" if vm.spec.get("runStrategy") == "Halted" else "Running"
    )
    pending = PROVISIONING_ANNOTATION in annotations
    grants_ready = not grants or annotations.get(KUBERNETES_GRANTS_READY_ANNOTATION) == "true"
    if pending or not grants_ready:
        state = ProvisioningState.WAITING_FOR_GRANTS
    elif mode == "Suspended":
        state = ProvisioningState.STOPPING if vmi is not None else ProvisioningState.SUSPENDED
    elif vmi is None:
        state = ProvisioningState.WAITING_FOR_VM
    elif pod is None:
        state = ProvisioningState.WAITING_FOR_POD
    elif not _pod_ready(pod) or vmi_status.get("phase") != "Running" or not _vmi_ready(vmi_status):
        state = ProvisioningState.WAITING_FOR_GUEST
    else:
        state = ProvisioningState.RUNNING
    vm_status = VirtualMachineStatus(
        phase=vmi_status.get("phase"),
        printable_status=vm.status.get("printableStatus"),
        vmi_uid=vmi.metadata.uid if vmi else None,
        guest_ip=vmi_status.get("interfaces", [{}])[0].get("ipAddress") if vmi_status.get("interfaces") else None,
        node_name=vmi_status.get("nodeName"),
        conditions=[*conditions, *vmi_conditions],
        reason=vmi_status.get("reason"),
        message=vmi_status.get("message"),
    )
    binding = (
        read_binding(annotations[SANDBOX_BINDING_ANNOTATION]) if SANDBOX_BINDING_ANNOTATION in annotations else None
    )
    return Sandbox(
        name=meta.name,
        uid=meta.uid,
        kind=EnvironmentKind.KUBEVIRT,
        template=annotations.get(VM_TEMPLATE_ANNOTATION, ""),
        capabilities=["stop_start"],
        state=state,
        created_at=created_at,
        operating_mode=mode,
        conditions=conditions,
        node_name=vmi_status.get("nodeName"),
        service_account=ServiceAccount(
            namespace=meta.namespace, name=annotations.get(LAUNCHER_SERVICE_ACCOUNT_ANNOTATION, "")
        ),
        binding=binding,
        kubernetes_grants=grants,
        kubernetes_grants_ready=grants_ready,
        kubernetes_grant_error=annotations.get(KUBERNETES_GRANTS_ERROR_ANNOTATION),
        deleting=meta.deletion_timestamp is not None,
        pod=_pod_status(pod) if pod is not None else None,
        vm=vm_status,
    )


def _condition(item: dict[str, Any]) -> Condition:
    return ParseDict(
        {key: value for key, value in item.items() if key in {"type", "status", "reason", "message"}}, Condition()
    )


def _pod_ready(pod: k8s_client.V1Pod) -> bool:
    return bool(
        pod.status
        and pod.status.phase == "Running"
        and any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or [])
    )


def _vmi_ready(status: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    )
