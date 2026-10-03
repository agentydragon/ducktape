"""Read-only projections of persisted Sandbox/Pod resources, shared with UI informers."""

import json
from collections.abc import Iterable
from datetime import datetime

from google.protobuf.json_format import ParseDict
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_grants import DnsName, KubernetesGrant
from agentplane.sandbox_service.models import OperatingMode, ProvisioningState
from agentplane.sandbox_service.protocol_pb2 import (
    Condition,
    ContainerStatus,
    PodStatus,
    ResolvedGrant,
    Sandbox,
    SandboxBinding,
    ServiceAccount,
)

MANAGED_LABEL = "agentplane.allegedly.works/managed"
TEMPLATE_ANNOTATION = "agentplane.allegedly.works/template"
SANDBOX_BINDING_ANNOTATION = "agentplane.allegedly.works/sandbox-binding"
PROVISIONING_ANNOTATION = "agentplane.allegedly.works/pending-launch-grants"
KUBERNETES_GRANTS_ANNOTATION = "agentplane.allegedly.works/kubernetes-grants"
KUBERNETES_GRANTS_READY_ANNOTATION = "agentplane.allegedly.works/kubernetes-grants-ready"
KUBERNETES_GRANTS_ERROR_ANNOTATION = "agentplane.allegedly.works/kubernetes-grants-error"


class _ObjectMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str
    uid: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: datetime = Field(alias="creationTimestamp")
    deletion_timestamp: datetime | None = Field(alias="deletionTimestamp", default=None)
    finalizers: list[str] = Field(default_factory=list)


class _PodSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Kubernetes' own default: a Pod naming no account runs as `default` in its namespace.
    service_account_name: str = Field(alias="serviceAccountName", default="default")


class _PodTemplate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spec: _PodSpec = Field(default_factory=_PodSpec)


class _SandboxSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The CRD defaults `operatingMode` to Running, so a stored Sandbox without it is a running one.
    operating_mode: OperatingMode = Field(alias="operatingMode", default=OperatingMode.RUNNING)
    pod_template: _PodTemplate = Field(alias="podTemplate", default_factory=_PodTemplate)


class _SandboxStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conditions: list[dict[str, object]] = Field(default_factory=list)
    node_name: str | None = Field(alias="nodeName", default=None)


class SandboxResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _ObjectMeta
    spec: _SandboxSpec
    status: _SandboxStatus = Field(default_factory=_SandboxStatus)


def sandbox_views(sandboxes: Iterable[object], pods: Iterable[k8s_client.V1Pod]) -> list[Sandbox]:
    """One row per Sandbox, each joined to the Pod of the same name."""
    pods_by_name = {pod.metadata.name: pod for pod in pods}
    views = []
    for item in sandboxes:
        parsed = SandboxResource.model_validate(item)
        views.append(_view(parsed, pods_by_name.get(parsed.metadata.name)))
    return views


def sandbox_view(sandbox: object, pod: k8s_client.V1Pod | None) -> Sandbox:
    return _view(SandboxResource.model_validate(sandbox), pod)


def _view(sandbox: SandboxResource, pod: k8s_client.V1Pod | None) -> Sandbox:
    grants = _resolved_grants(sandbox)
    created_at = Timestamp()
    created_at.FromDatetime(sandbox.metadata.creation_timestamp)
    return Sandbox(
        name=sandbox.metadata.name,
        uid=sandbox.metadata.uid,
        kind="agent_sandbox",
        template=sandbox.metadata.annotations.get(TEMPLATE_ANNOTATION, ""),
        capabilities=["pod_exec", "stop_start"],
        state=_state(sandbox, pod),
        created_at=created_at,
        operating_mode=sandbox.spec.operating_mode,
        service_account=ServiceAccount(
            namespace=sandbox.metadata.namespace, name=sandbox.spec.pod_template.spec.service_account_name
        ),
        conditions=[
            ParseDict({k: v for k, v in c.items() if k in {"type", "status", "reason", "message"}}, Condition())
            for c in sandbox.status.conditions
        ],
        node_name=sandbox.status.node_name,
        binding=_binding(sandbox),
        kubernetes_grants=grants,
        kubernetes_grants_ready=not grants
        or sandbox.metadata.annotations.get(KUBERNETES_GRANTS_READY_ANNOTATION) == "true",
        kubernetes_grant_error=sandbox.metadata.annotations.get(KUBERNETES_GRANTS_ERROR_ANNOTATION),
        deleting=sandbox.metadata.deletion_timestamp is not None,
        pod=_pod_status(pod) if pod is not None else None,
    )


def _binding(sandbox: SandboxResource) -> SandboxBinding | None:
    raw = sandbox.metadata.annotations.get(SANDBOX_BINDING_ANNOTATION)
    if raw is None:
        return None
    return read_binding(raw)


def _resolved_grants(sandbox: SandboxResource) -> list[ResolvedGrant]:
    raw = sandbox.metadata.annotations.get(KUBERNETES_GRANTS_ANNOTATION)
    if raw is None:
        return []
    result = []
    for item in json.loads(raw):
        TypeAdapter(DnsName).validate_python(item["name"])
        TypeAdapter(KubernetesGrant).validate_python(item["grant"])
        result.append(ParseDict(item, ResolvedGrant()))
    return result


def _pod_status(pod: k8s_client.V1Pod) -> PodStatus:
    status = pod.status if pod.status is not None else k8s_client.V1PodStatus()
    return PodStatus(
        phase=status.phase,
        ip=status.pod_ip,
        node_name=pod.spec.node_name if pod.spec is not None else None,
        reason=status.reason,
        message=status.message,
        conditions=[
            Condition(type=condition.type, status=condition.status, reason=condition.reason, message=condition.message)
            for condition in status.conditions or []
        ],
        containers=[_container_status(container) for container in status.container_statuses or []],
    )


def _container_status(container: k8s_client.V1ContainerStatus) -> ContainerStatus:
    # Exactly one of the three is set by the kubelet; a status with none is a container not yet scheduled.
    state = container.state if container.state is not None else k8s_client.V1ContainerState()
    if state.waiting is not None:
        name, reason, message = "waiting", state.waiting.reason, state.waiting.message
    elif state.terminated is not None:
        name, reason, message = "terminated", state.terminated.reason, state.terminated.message
    elif state.running is not None:
        name, reason, message = "running", None, None
    else:
        name, reason, message = "waiting", None, None
    return ContainerStatus(
        name=container.name,
        state=name,
        reason=reason,
        message=message,
        ready=container.ready,
        restart_count=container.restart_count,
    )


def _state(sandbox: SandboxResource, pod: k8s_client.V1Pod | None) -> ProvisioningState:
    if sandbox.spec.operating_mode == OperatingMode.SUSPENDED:
        return ProvisioningState.SUSPENDED
    if pod is None:
        return ProvisioningState.WAITING_FOR_POD
    if PROVISIONING_ANNOTATION in sandbox.metadata.annotations:
        return ProvisioningState.WAITING_FOR_GRANTS
    if _resolved_grants(sandbox) and sandbox.metadata.annotations.get(KUBERNETES_GRANTS_READY_ANNOTATION) != "true":
        return ProvisioningState.WAITING_FOR_GRANTS
    return ProvisioningState.RUNNING if _pod_ready(pod) else ProvisioningState.WAITING_FOR_POD_READY


def _pod_ready(pod: k8s_client.V1Pod) -> bool:
    if pod.status is None or pod.status.conditions is None:
        return False
    return any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions)
