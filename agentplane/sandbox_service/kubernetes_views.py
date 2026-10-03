"""Read-only projections of persisted Sandbox/Pod resources, shared with UI informers."""

import json
from collections.abc import Iterable
from datetime import datetime

from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_grants import DnsName, KubernetesGrant
from agentplane.sandbox_service.models import OperatingMode
from agentplane.sandbox_service.protocol_pb2 import (
    OwnerReference,
    ResolvedGrant,
    Sandbox,
    SandboxBinding,
    SandboxPod,
    ServiceAccount,
)

MANAGED_LABEL = "agentplane.allegedly.works/managed"
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


class SandboxResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: _ObjectMeta
    spec: _SandboxSpec
    status: dict[str, object] | None = None


def sandbox_views(
    sandboxes: Iterable[object], pods: Iterable[k8s_client.V1Pod], *, api_client: k8s_client.ApiClient
) -> list[Sandbox]:
    """One row per Sandbox, each joined to the Pod of the same name."""
    pods_by_name = {pod.metadata.name: pod for pod in pods}
    views = []
    for item in sandboxes:
        parsed = SandboxResource.model_validate(item)
        views.append(_view(parsed, pods_by_name.get(parsed.metadata.name), api_client=api_client))
    return views


def sandbox_view(sandbox: object, pod: k8s_client.V1Pod | None, *, api_client: k8s_client.ApiClient) -> Sandbox:
    return _view(SandboxResource.model_validate(sandbox), pod, api_client=api_client)


def _view(sandbox: SandboxResource, pod: k8s_client.V1Pod | None, *, api_client: k8s_client.ApiClient) -> Sandbox:
    grants = _resolved_grants(sandbox)
    created_at = Timestamp()
    created_at.FromDatetime(sandbox.metadata.creation_timestamp)
    return Sandbox(
        name=sandbox.metadata.name,
        uid=sandbox.metadata.uid,
        created_at=created_at,
        operating_mode=sandbox.spec.operating_mode,
        namespace=sandbox.metadata.namespace,
        status=_struct(sandbox.status) if sandbox.status is not None else None,
        launch_grants_pending=PROVISIONING_ANNOTATION in sandbox.metadata.annotations,
        service_account=ServiceAccount(
            namespace=sandbox.metadata.namespace, name=sandbox.spec.pod_template.spec.service_account_name
        ),
        binding=_binding(sandbox),
        kubernetes_grants=grants,
        kubernetes_grants_ready=not grants
        or sandbox.metadata.annotations.get(KUBERNETES_GRANTS_READY_ANNOTATION) == "true",
        kubernetes_grant_error=sandbox.metadata.annotations.get(KUBERNETES_GRANTS_ERROR_ANNOTATION),
        deleting=sandbox.metadata.deletion_timestamp is not None,
        pod=_pod(pod, api_client=api_client) if pod is not None else None,
    )


def _struct(value: dict[str, object]) -> Struct:
    return ParseDict(value, Struct())


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


def _pod(pod: k8s_client.V1Pod, *, api_client: k8s_client.ApiClient) -> SandboxPod:
    result = SandboxPod(
        name=pod.metadata.name or "",
        namespace=pod.metadata.namespace or "",
        uid=pod.metadata.uid or "",
        deleting=pod.metadata.deletion_timestamp is not None,
        node_name=pod.spec.node_name if pod.spec is not None else None,
        owner_references=[
            OwnerReference(
                api_version=owner.api_version or "",
                kind=owner.kind or "",
                name=owner.name or "",
                uid=owner.uid or "",
                controller=owner.controller is True,
            )
            for owner in pod.metadata.owner_references or []
        ],
    )
    if pod.status is not None:
        status = api_client.sanitize_for_serialization(pod.status)
        if not isinstance(status, dict):
            raise TypeError(f"Kubernetes serialized Pod status as {type(status).__name__}, not dict")
        result.status.SetInParent()
        ParseDict(status, result.status)
    return result
