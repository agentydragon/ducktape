"""Read-only projections of persisted Sandbox/Pod resources, shared with UI informers."""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import cast

from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP
from agentplane.proto.sandbox_service_pb2 import (
    OwnerReference,
    ResolvedGrant,
    Sandbox,
    SandboxBinding,
    SandboxPod,
    ServiceAccount,
    SessionDefaults,
    TeardownPhase,
)
from agentplane.sandbox_service.binding_storage import read_binding
from agentplane.sandbox_service.kubernetes_grants import DnsName, KubernetesGrant
from util.agent_sandbox import OperatingMode

MANAGED_LABEL = f"{GROUP}/managed"
CREATE_INTENT = f"{GROUP}/create-intent"
INITIALIZING = f"{GROUP}/initializing"
SANDBOX_BINDING_ANNOTATION = f"{GROUP}/sandbox-binding"
PROVISIONING_ANNOTATION = f"{GROUP}/pending-launch-grants"
KUBERNETES_GRANTS_ANNOTATION = f"{GROUP}/kubernetes-grants"
KUBERNETES_GRANTS_READY_ANNOTATION = f"{GROUP}/kubernetes-grants-ready"
KUBERNETES_GRANTS_ERROR_ANNOTATION = f"{GROUP}/kubernetes-grants-error"
# A `TeardownPhase` name, set by DeleteSandbox.
TEARDOWN_ANNOTATION = f"{GROUP}/teardown"


class _KubernetesModel(BaseModel):
    """Read Kubernetes camelCase objects without hand-maintained aliases."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel)


class _ObjectMeta(_KubernetesModel):
    name: str
    namespace: str
    uid: str
    resource_version: str = Field(alias="resourceVersion", default="")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: datetime
    deletion_timestamp: datetime | None = Field(default=None)
    finalizers: list[str] = Field(default_factory=list)


class _PodSpec(_KubernetesModel):
    # Kubernetes' own default: a Pod naming no account runs as `default` in its namespace.
    service_account_name: str = Field(default="default")


class _PodTemplate(_KubernetesModel):
    spec: _PodSpec = Field(default_factory=_PodSpec)


class _SandboxSpec(_KubernetesModel):
    # The CRD defaults `operatingMode` to Running, so a stored Sandbox without it is a running one.
    operating_mode: OperatingMode = Field(default=OperatingMode.RUNNING)
    pod_template: _PodTemplate = Field(default_factory=_PodTemplate)


class SandboxResource(_KubernetesModel):
    metadata: _ObjectMeta
    spec: _SandboxSpec
    status: dict[str, object] | None = None

    @property
    def teardown(self) -> TeardownPhase:
        raw = self.metadata.annotations.get(TEARDOWN_ANNOTATION)
        return (
            TeardownPhase.TEARDOWN_PHASE_UNSPECIFIED if raw is None else cast(TeardownPhase, TeardownPhase.Value(raw))
        )


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


def own_sandbox_views(
    sandboxes: Iterable[object], pods: Iterable[k8s_client.V1Pod], *, api_client: k8s_client.ApiClient
) -> list[Sandbox]:
    """`sandbox_views` for Agentplane's own Sandboxes."""
    pods_by_name = {pod.metadata.name: pod for pod in pods}
    return [
        own_sandbox_view(parsed, incarnation_pod(parsed, pods_by_name), api_client=api_client)
        for parsed in map(sandbox_resource.Sandbox.model_validate, sandboxes)
    ]


def incarnation_pod(
    sandbox: sandbox_resource.Sandbox, pods_by_name: Mapping[str, k8s_client.V1Pod]
) -> k8s_client.V1Pod | None:
    """The Sandbox's incarnation among `pods_by_name`, if it is there."""
    incarnation = sandbox.status.incarnation
    return None if incarnation is None else pods_by_name.get(incarnation.name)


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
        initializing=INITIALIZING in sandbox.metadata.annotations,
        service_account=ServiceAccount(
            namespace=sandbox.metadata.namespace, name=sandbox.spec.pod_template.spec.service_account_name
        ),
        binding=_binding(sandbox),
        kubernetes_grants=grants,
        kubernetes_grants_ready=not grants
        or sandbox.metadata.annotations.get(KUBERNETES_GRANTS_READY_ANNOTATION) == "true",
        kubernetes_grant_error=sandbox.metadata.annotations.get(KUBERNETES_GRANTS_ERROR_ANNOTATION),
        deleting=sandbox.metadata.deletion_timestamp is not None,
        teardown=sandbox.teardown,
        pod=_pod(pod, api_client=api_client) if pod is not None else None,
    )


def own_sandbox_view(
    sandbox: sandbox_resource.Sandbox, pod: k8s_client.V1Pod | None, *, api_client: k8s_client.ApiClient
) -> Sandbox:
    """Agentplane's own Sandbox in the same projection. Its Pod is its incarnation (its own, or its
    VM's virt-launcher Pod), and `pod` is shown only while it is that one. It runs as the
    ServiceAccount of its name, which the Sandbox Controller binds; nothing is pending past that, since no Pod waits for its
    grants. While it seals before deletion it is in teardown, as an agent-sandbox one is from
    DeleteSandbox on, and it is deleting once its Sessions are sealed."""
    metadata, spec = sandbox.metadata, sandbox.spec
    created_at = Timestamp()
    created_at.FromDatetime(metadata.creation_timestamp)
    in_deletion = metadata.deletion_timestamp is not None
    return Sandbox(
        name=metadata.name,
        uid=str(metadata.uid),
        created_at=created_at,
        operating_mode=OperatingMode.RUNNING if spec.running else OperatingMode.SUSPENDED,
        namespace=metadata.namespace,
        status=_struct(sandbox.status.model_dump(mode="json", by_alias=True, exclude_none=True)),
        service_account=ServiceAccount(namespace=metadata.namespace, name=metadata.name),
        binding=_own_binding(spec),
        kubernetes_grants_ready=True,
        deleting=in_deletion and sandbox.sessions_sealed,
        teardown=TeardownPhase.TEARDOWN_PHASE_SEALING
        if in_deletion and not sandbox.sessions_sealed
        else TeardownPhase.TEARDOWN_PHASE_UNSPECIFIED,
        pod=_pod(pod, api_client=api_client) if pod is not None and sandbox.runs_in(pod) else None,
    )


def _own_binding(spec: sandbox_resource.Spec) -> SandboxBinding | None:
    if spec.session_defaults is None and spec.bootstrap is None:
        return None
    return SandboxBinding(
        session_defaults=None
        if spec.session_defaults is None
        else ParseDict(spec.session_defaults.model_dump(by_alias=True, exclude_none=True), SessionDefaults()),
        bootstrap=spec.bootstrap or "",
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
