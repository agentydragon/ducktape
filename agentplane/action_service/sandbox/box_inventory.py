"""The boxes a `box` group creates, reads and deletes, and the commands it runs in them.

A box is a plain Pod running as its caller, stamped from a PodTemplate the binding offers; its labels,
ownership check and errors are the `sandbox` inventory's (inventory.py). It is not a Sandbox Service
Sandbox (<../../docs/adr_action_sandboxes.md>): no runner, no state volume, no
ServiceAccount of its own. Each carries an owner reference to its caller's ServiceAccount, so a
Session Sandbox's agent that created boxes as that Sandbox's account loses them with the Sandbox.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    CoreV1Api,
    V1ObjectMeta,
    V1OwnerReference,
    V1Pod,
    V1PodTemplate,
    V1PodTemplateSpec,
)

from agentplane.action_service.sandbox.binding import BoxExecutorBinding
from agentplane.action_service.sandbox.inventory import (
    CALLER_LABEL,
    CALLER_NAMESPACE_LABEL,
    DEFAULT_CONTAINER_ANNOTATION,
    MANAGED_LABEL,
    NAME_LABEL,
    TEMPLATE_LABEL,
    ForeignSandboxError,
    SandboxActionError,
    labels,
    object_name,
)
from agentplane.action_service.sandbox.models import READY_CONDITION, SandboxCondition, SandboxInfo
from agentplane.egress import sidecar
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult, ExecRunner

logger = logging.getLogger(__name__)


def _workload_container(pod: V1Pod) -> str:
    """The container commands run in, by the rule `kubectl exec` follows: the one the Pod names as
    its default, else its first."""
    annotations = pod.metadata.annotations or {}
    return cast(str, annotations.get(DEFAULT_CONTAINER_ANNOTATION) or pod.spec.containers[0].name)


def _conditions(pod: V1Pod) -> list[SandboxCondition]:
    return [
        SandboxCondition(
            type=condition.type,
            status=condition.status,
            reason=condition.reason,
            message=condition.message,
            last_transition_time=condition.last_transition_time,
        )
        for condition in (pod.status.conditions if pod.status is not None else None) or []
    ]


def _info(pod: V1Pod) -> SandboxInfo:
    pod_labels = pod.metadata.labels
    return SandboxInfo(
        name=pod_labels[NAME_LABEL],
        template=pod_labels[TEMPLATE_LABEL],
        phase=pod.status.phase if pod.status is not None else None,
        conditions=_conditions(pod),
        created_at=pod.metadata.creation_timestamp,
        pod_name=pod.metadata.name,
        node_name=pod.spec.node_name,
        pod_ips=[address.ip for address in (pod.status.pod_ips if pod.status is not None else None) or []],
    )


def _check_box(name: str, template: V1PodTemplateSpec) -> None:
    """Refuse a template whose box would escape the egress path. A box runs as its caller, so a
    ServiceAccount token in the container an agent runs commands in would hand out the caller's
    whole authority as a portable credential (README.md)."""
    spec = template.spec
    if spec.automount_service_account_token is not False:
        raise ValueError(f"box template {name!r} must set automountServiceAccountToken: false")
    if sidecar.CONTAINER_NAME not in {container.name for container in spec.containers}:
        raise ValueError(f"box template {name!r} must carry the {sidecar.CONTAINER_NAME!r} container")
    token_volumes = {
        volume.name
        for volume in spec.volumes or []
        if volume.projected is not None
        and any(source.service_account_token is not None for source in volume.projected.sources or [])
    }
    for container in spec.containers:
        if container.name != sidecar.CONTAINER_NAME and (
            mounted := token_volumes & {mount.name for mount in container.volume_mounts or []}
        ):
            raise ValueError(
                f"box template {name!r}: {container.name!r} mounts ServiceAccount tokens {sorted(mounted)}"
            )


def _refusal(error: ApiException) -> str:
    """The API server's own sentence for a refused create, such as an exceeded quota, from its Status."""
    return cast(str, json.loads(error.body)["message"]) if error.body else str(error.reason)


class BoxInventory:
    """Every box this group owns, keyed by the caller that asked for it."""

    def __init__(
        self, binding: BoxExecutorBinding, *, api: ApiClient, core_v1: CoreV1Api, exec_runner: ExecRunner
    ) -> None:
        self._binding = binding
        self._api = api
        self._core_v1 = core_v1
        self._exec_runner = exec_runner

    async def _template(self, name: str) -> V1PodTemplateSpec:
        """An offered template's Pod, read now: the image pins move under a running service."""
        if name not in self._binding.templates:
            offered = ", ".join(sorted(self._binding.templates))
            raise SandboxActionError(f"unknown template {name!r}; this deployment offers {offered}")
        template = cast(V1PodTemplate, await self._core_v1.read_namespaced_pod_template(name, self._binding.namespace))
        _check_box(name, template.template)
        return cast(V1PodTemplateSpec, template.template)

    async def check_templates(self) -> None:
        """Read every offered template once, when the service starts: one that is missing, or whose
        box would escape the egress path, is a deployment defect, and refusing to start names it
        where a caller would only meet it as a box that cannot be created."""
        for name in sorted(self._binding.templates):
            await self._template(name)

    async def get_template(self, name: str) -> dict[str, Any]:
        """The Pod an offered template gives each box, as Kubernetes JSON. Every caller may read all
        of it, which is why a template references Secrets and never carries one."""
        template = await self._template(name)
        return cast(dict[str, Any], self._api.sanitize_for_serialization(template))

    async def _pod(self, caller: ServiceAccountRef, name: str) -> V1Pod | None:
        """The caller's box of that name, or None. A same-named Pod this surface did not create for
        this caller raises rather than being adopted or reported as absent."""
        try:
            pod = await self._core_v1.read_namespaced_pod(object_name(caller, name), self._binding.namespace)
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        pod_labels = pod.metadata.labels or {}
        if (
            pod_labels.get(MANAGED_LABEL) != "true"
            or pod_labels.get(CALLER_LABEL) != caller.name
            or pod_labels.get(CALLER_NAMESPACE_LABEL) != caller.namespace
        ):
            raise ForeignSandboxError(f"a sandbox named {name!r} exists and is not yours")
        return cast(V1Pod, pod)

    async def create(self, caller: ServiceAccountRef, name: str, template: str) -> SandboxInfo:
        """Start the caller's box if it has none by that name, and report where it got to.

        Returns once the Pod exists rather than waiting for it to come up: an image pull can outlast
        the execution lease the Action Service grants, so waiting here reports an unknown outcome
        for a box that is in fact fine. `get` is how a caller follows one to ready.

        Idempotent: an existing box of that name is returned as it stands, whatever template it was
        created from. Deciding that the shape is wrong is the caller's, and `dispose` is how it acts
        on that.
        """
        box = await self._template(template)
        if (existing := await self._pod(caller, name)) is None:
            await self._stamp(caller, name, template, box)
        elif (existing_template := existing.metadata.labels[TEMPLATE_LABEL]) != template:
            logger.info("sandbox %r exists from template %r; returning it as it stands", name, existing_template)
        pod = await self._pod(caller, name)
        if pod is None:
            raise SandboxActionError(f"sandbox {name!r} disappeared as it was created")
        return _info(pod)

    async def _stamp(
        self, caller: ServiceAccountRef, name: str, template_name: str, template: V1PodTemplateSpec
    ) -> None:
        account = await self._core_v1.read_namespaced_service_account(caller.name, self._binding.namespace)
        metadata = template.metadata or V1ObjectMeta()
        template.spec.service_account_name = caller.name
        body = V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=V1ObjectMeta(
                name=object_name(caller, name),
                labels={**(metadata.labels or {}), **labels(caller, name, template_name)},
                annotations=metadata.annotations,
                # Collected with the account: a Session Sandbox's account goes with the Sandbox, and
                # so do the boxes its agent made.
                owner_references=[
                    V1OwnerReference(
                        api_version="v1", kind="ServiceAccount", name=caller.name, uid=account.metadata.uid
                    )
                ],
            ),
            # TODO(sandbox-lifetime): a forgotten box keeps its share of the namespace's quota until
            # someone disposes it. Expire boxes nobody has exec'd into for a while.
            spec=template.spec,
        )
        try:
            await self._core_v1.create_namespaced_pod(self._binding.namespace, body)
        except ApiException as error:
            match error.status:
                case 409:
                    # Another replica stamped the same box; its Pod is as good as ours.
                    return
                case 403 | 422:
                    raise SandboxActionError(
                        f"the API server refused sandbox {name!r} ({error.status} {error.reason}): {_refusal(error)}"
                    ) from error
            raise

    async def get(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        pod = await self._pod(caller, name)
        if pod is None:
            raise SandboxActionError(f"no sandbox named {name!r}; create it first")
        return _info(pod)

    async def list(self, caller: ServiceAccountRef) -> list[SandboxInfo]:
        pods = await self._core_v1.list_namespaced_pod(
            self._binding.namespace,
            label_selector=(
                f"{MANAGED_LABEL}=true,{CALLER_LABEL}={caller.name},{CALLER_NAMESPACE_LABEL}={caller.namespace}"
            ),
        )
        return [_info(pod) for pod in pods.items]

    async def dispose(self, caller: ServiceAccountRef, name: str) -> bool:
        """Delete the caller's box; False when there was none to delete."""
        if await self._pod(caller, name) is None:
            return False
        try:
            await self._core_v1.delete_namespaced_pod(object_name(caller, name), self._binding.namespace)
        except ApiException as error:
            if error.status == 404:
                return False
            raise
        return True

    async def execute(
        self,
        caller: ServiceAccountRef,
        name: str,
        *,
        script: str,
        cwd: str | None,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> CommandResult:
        pod = await self._pod(caller, name)
        if pod is None:
            raise SandboxActionError(f"no sandbox named {name!r}; create it first")
        ready = next((condition for condition in _conditions(pod) if condition.type == READY_CONDITION), None)
        if ready is None or ready.status != "True":
            said = ready.message or ready.reason if ready is not None else None
            raise SandboxActionError(f"sandbox {name!r} is not ready ({said or 'Kubernetes has not said why'})")
        return await self._exec_runner.run(
            pod_name=pod.metadata.name,
            namespace=self._binding.namespace,
            container=_workload_container(pod),
            script=script,
            cwd=cwd,
            # Submission already refuses past these caps; a request another replica admitted under
            # different caps, across a settings roll, is still held to this one's.
            max_output_bytes=min(max_output_bytes, self._binding.max_output_bytes),
            timeout_seconds=min(timeout_seconds, self._binding.max_timeout_seconds),
        )
