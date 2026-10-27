"""Read-only projections of persisted Sandbox/Pod resources, shared with UI informers."""

from collections.abc import Iterable, Mapping

from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP
from agentplane.proto.sandboxes_pb2 import (
    OwnerReference,
    Sandbox,
    SandboxBinding,
    SandboxPhase,
    SandboxPod,
    ServiceAccount,
)
from agentplane.sandbox_resource import OperatingMode

# Must equal CreateIntentAnnotation in agentplane/sandboxes/api/constants.go.
CREATE_INTENT = f"{GROUP}/create-intent"


def sandbox_views(
    sandboxes: Iterable[object], pods: Iterable[k8s_client.V1Pod], *, api_client: k8s_client.ApiClient
) -> list[Sandbox]:
    """One row per Sandbox, each joined to its incarnation's Pod."""
    pods_by_name = {pod.metadata.name: pod for pod in pods}
    return [
        sandbox_view(parsed, incarnation_pod(parsed, pods_by_name), api_client=api_client)
        for parsed in map(sandbox_resource.Sandbox.model_validate, sandboxes)
    ]


def incarnation_pod(
    sandbox: sandbox_resource.Sandbox, pods_by_name: Mapping[str, k8s_client.V1Pod]
) -> k8s_client.V1Pod | None:
    """The Sandbox's incarnation among `pods_by_name`, if it is there."""
    incarnation = sandbox.status.incarnation
    return None if incarnation is None else pods_by_name.get(incarnation.name)


def sandbox_view(
    sandbox: sandbox_resource.Sandbox, pod: k8s_client.V1Pod | None, *, api_client: k8s_client.ApiClient
) -> Sandbox:
    """Its Pod is its incarnation (its own, or its VM's virt-launcher Pod), and `pod` is shown only
    while it is that one. It runs as the ServiceAccount of its name, which the Sandbox Controller
    binds."""
    metadata, spec = sandbox.metadata, sandbox.spec
    created_at = Timestamp()
    created_at.FromDatetime(metadata.creation_timestamp)
    view = Sandbox(
        name=metadata.name,
        uid=str(metadata.uid),
        created_at=created_at,
        namespace=metadata.namespace,
        status=_struct(sandbox.status.model_dump(mode="json", by_alias=True, exclude_none=True)),
        service_account=ServiceAccount(namespace=metadata.namespace, name=metadata.name),
        binding=_binding(spec),
        preset=metadata.labels.get(sandbox_resource.PRESET_LABEL),
        wake_on_send=spec.wake_on_send,
        phase=_phase(sandbox),
        pod=_pod(pod, api_client=api_client) if pod is not None and sandbox.runs_in(pod) else None,
    )
    if sandbox.operating_mode is not None:
        view.operating_mode = sandbox.operating_mode.value
    if sandbox.status.suspended_by is not None:
        view.suspended_by = sandbox.status.suspended_by.value
    if spec.idle_suspend_after is not None:
        view.idle_suspend_after.FromTimedelta(spec.idle_suspend_after)
    return view


def _phase(sandbox: sandbox_resource.Sandbox) -> SandboxPhase:
    """The `Running` reasons `Quiescing` and `Deleting` are the Sandbox
    Controller's: it holds a stop or a deletion for the seal, whoever seals, then deletes the workload."""
    if sandbox.metadata.deletion_timestamp is not None:
        sealed = sandbox.sessions_sealed or sandbox.running_reason() == "Deleting"
        return SandboxPhase.SANDBOX_PHASE_TEARING_DOWN if sealed else SandboxPhase.SANDBOX_PHASE_QUIESCING
    if sandbox.operating_mode is None:
        return SandboxPhase.SANDBOX_PHASE_UNSPECIFIED
    incarnated = sandbox.status.incarnation is not None
    if sandbox.operating_mode is OperatingMode.RUNNING:
        return SandboxPhase.SANDBOX_PHASE_RUNNING if incarnated else SandboxPhase.SANDBOX_PHASE_CREATING
    if not incarnated:
        return SandboxPhase.SANDBOX_PHASE_SUSPENDED
    return (
        SandboxPhase.SANDBOX_PHASE_QUIESCING
        if sandbox.running_reason() == "Quiescing"
        else SandboxPhase.SANDBOX_PHASE_SUSPENDING
    )


def _binding(spec: sandbox_resource.Spec) -> SandboxBinding | None:
    if spec.bootstrap is None:
        return None
    return SandboxBinding(bootstrap=spec.bootstrap)


def _struct(value: dict[str, object]) -> Struct:
    return ParseDict(value, Struct())


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
