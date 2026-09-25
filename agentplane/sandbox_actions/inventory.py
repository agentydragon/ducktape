"""Sandboxes this surface stamps, reads and deletes, and the commands it runs in them.

Its own inventory rather than the integration app's (<../app/inventory.py>): that one mints a
ServiceAccount per Sandbox and owns it, stamps the app's `managed` label, and is read back by the
app's fleet view and ingestion coordinator, which would then look for a runner these boxes do not
have. What is shared is the CRD, not the code.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiException, CoreV1Api

from agentplane.sandbox_actions.binding import DESCRIPTION_ANNOTATION, PREFIX, SandboxExecutorBinding
from agentplane.sandbox_actions.models import READY_CONDITION, SandboxInfo
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult, ExecRunner
from util.agent_sandbox import EXTENSIONS_API, SANDBOX_API, SANDBOXES_PLURAL, TEMPLATES_PLURAL, condition, pod_name
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)

# What this surface will touch. The Action Service's `pods/exec` grant is namespace-wide and cannot
# express "only the boxes this Action made", so the boundary is here: every read, exec and delete
# selects on this label and on the caller's, and a Sandbox carrying neither is not ours to act on.
MANAGED_LABEL = f"{PREFIX}/managed"
CALLER_LABEL = f"{PREFIX}/caller"
CALLER_NAMESPACE_LABEL = f"{PREFIX}/caller-namespace"
TEMPLATE_LABEL = f"{PREFIX}/template"
NAME_LABEL = f"{PREFIX}/name"
# The annotation `kubectl exec` and `kubectl logs` pick a Pod's container by when none is named.
DEFAULT_CONTAINER_ANNOTATION = "kubectl.kubernetes.io/default-container"


class SandboxActionError(Exception):
    """The request cannot be carried out, in terms the calling agent can act on."""


class ForeignSandboxError(SandboxActionError):
    """A Sandbox of that name exists and belongs to someone else, or to nothing this surface made."""


def _labels(caller: ServiceAccountRef, name: str, template: str) -> dict[str, str]:
    return {
        MANAGED_LABEL: "true",
        CALLER_LABEL: caller.name,
        CALLER_NAMESPACE_LABEL: caller.namespace,
        TEMPLATE_LABEL: template,
        NAME_LABEL: name,
    }


def _object_name(caller: ServiceAccountRef, name: str) -> str:
    """Deterministic, so creating the same name twice reaches the same box rather than a second one."""
    return f"{caller.name}-{name}"[:63].rstrip("-")


def _workload_container(sandbox: dict[str, Any]) -> str:
    """The container commands run in, by the rule `kubectl exec` follows: the one the box's Pod
    template names as its default, else its first."""
    pod_template = sandbox["spec"]["podTemplate"]
    annotations = pod_template.get("metadata", {}).get("annotations", {})
    return cast(str, annotations.get(DEFAULT_CONTAINER_ANNOTATION) or pod_template["spec"]["containers"][0]["name"])


def _ready(sandbox: dict[str, Any]) -> dict[str, Any] | None:
    """The controller's Ready condition when it says yes, else None.

    The one reading this surface takes, because `exec` has to decide something; every condition
    reaches the caller untouched either way. Re-deciding readiness here from Pod phase and
    container statuses would be a weaker copy that can disagree with the authority.
    """
    ready = condition(sandbox, READY_CONDITION)
    return ready if ready is not None and ready.get("status") == "True" else None


class SandboxInventory:
    """Every Sandbox this surface owns, keyed by the caller that asked for it."""

    def __init__(
        self,
        binding: SandboxExecutorBinding,
        *,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        exec_runner: ExecRunner,
    ) -> None:
        self._binding = binding
        self._custom_objects = custom_objects
        self._core_v1 = core_v1
        self._exec_runner = exec_runner

    def _require_offered(self, template: str) -> None:
        if template not in self._binding.templates:
            offered = ", ".join(sorted(self._binding.templates))
            raise SandboxActionError(f"unknown template {template!r}; this deployment offers {offered}")

    async def _read_template(self, name: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._custom_objects.get_namespaced_custom_object(
                *EXTENSIONS_API, self._binding.namespace, TEMPLATES_PLURAL, name
            ),
        )

    async def get_template(self, name: str) -> dict[str, Any]:
        """An offered SandboxTemplate as the API server holds it, less the field-ownership record
        (`metadata.managedFields`) that `kubectl get` leaves out too.

        The rest goes out verbatim, read now rather than at startup. Every caller may read all of
        it, which is why a template references Secrets and never carries one.
        """
        self._require_offered(name)
        template = await self._read_template(name)
        template["metadata"].pop("managedFields", None)
        return template

    async def template_descriptions(self) -> dict[str, str]:
        """What each offered template says a box made from it holds, from its own annotation.

        Read once, when the service starts: an offered template that is missing or says nothing
        is a deployment defect, and refusing to start names it where a caller would only meet it
        as a box that cannot be created.
        """
        descriptions = {}
        for name in sorted(self._binding.templates):
            template = await self._read_template(name)
            description = template["metadata"].get("annotations", {}).get(DESCRIPTION_ANNOTATION)
            if not description:
                raise ValueError(f"offered SandboxTemplate {name!r} has no {DESCRIPTION_ANNOTATION!r} annotation")
            descriptions[name] = description
        return descriptions

    async def _sandbox(self, caller: ServiceAccountRef, name: str) -> dict[str, Any] | None:
        """The caller's Sandbox of that name, or None. A same-named object this surface did not
        create for this caller raises rather than being adopted or reported as absent."""
        try:
            sandbox = cast(
                dict[str, Any],
                await self._custom_objects.get_namespaced_custom_object(
                    *SANDBOX_API, self._binding.namespace, SANDBOXES_PLURAL, _object_name(caller, name)
                ),
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        labels = sandbox.get("metadata", {}).get("labels", {})
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(CALLER_LABEL) != caller.name
            or labels.get(CALLER_NAMESPACE_LABEL) != caller.namespace
        ):
            raise ForeignSandboxError(f"a sandbox named {name!r} exists and is not yours")
        return sandbox

    async def _pod_name(self, sandbox: dict[str, Any]) -> str | None:
        """The Pod backing this Sandbox, or None while the controller has not made one.

        Confirms the Pod exists rather than trusting the annotation: a Sandbox that has been Ready
        still names a Pod that a node drain or an eviction has since taken away, and `exec` needs
        the one that is there now.
        """
        name = pod_name(sandbox)
        try:
            await self._core_v1.read_namespaced_pod(name, self._binding.namespace)
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        return name

    async def _info(self, caller: ServiceAccountRef, sandbox: dict[str, Any]) -> SandboxInfo:
        metadata = sandbox["metadata"]
        status = cast(dict[str, Any], sandbox.get("status") or {})
        return SandboxInfo(
            name=metadata["labels"][NAME_LABEL],
            template=metadata["labels"][TEMPLATE_LABEL],
            conditions=cast(list[Any], status.get("conditions") or []),
            created_at=metadata.get("creationTimestamp"),
            # Only for the name to exec into; whether the box is usable is the conditions' answer.
            pod_name=await self._pod_name(sandbox) if _ready(sandbox) is not None else None,
            node_name=status.get("nodeName"),
            pod_ips=cast(list[str], status.get("podIPs") or []),
        )

    async def create(self, caller: ServiceAccountRef, name: str, template: str) -> SandboxInfo:
        """Stamp the caller's sandbox if it has none by that name, and report where it got to.

        Returns once the object exists rather than waiting for the box to come up: a cold start
        outlasts the execution lease the Action Service grants, so waiting here reports an unknown
        outcome for a box that is in fact fine. `info` is how a caller follows one from `not_ready`
        to `ready`, carrying the controller's own reason.

        Idempotent: an existing sandbox of that name is returned as it stands, whatever template it
        was created from. Deciding that the shape is wrong is the caller's, and `dispose` is how it
        acts on that.
        """
        self._require_offered(template)
        if (existing := await self._sandbox(caller, name)) is None:
            await self._stamp(caller, name, template)
        elif (existing_template := existing["metadata"]["labels"][TEMPLATE_LABEL]) != template:
            logger.info("sandbox %r exists from template %r; returning it as it stands", name, existing_template)
        sandbox = await self._sandbox(caller, name)
        if sandbox is None:
            raise SandboxActionError(f"sandbox {name!r} disappeared as it was created")
        return await self._info(caller, sandbox)

    async def _stamp(self, caller: ServiceAccountRef, name: str, template_name: str) -> None:
        template = await self._read_template(template_name)
        pod_template = cast(dict[str, Any], template["spec"]["podTemplate"])
        spec = {**cast(dict[str, Any], pod_template.get("spec", {})), "serviceAccountName": caller.name}
        body = {
            "apiVersion": SANDBOX_API.api_version,
            "kind": "Sandbox",
            "metadata": {"name": _object_name(caller, name), "labels": _labels(caller, name, template_name)},
            # Retain: this surface owns deletion, and a box whose caller is still working in it must
            # not be collected out from under them on a schedule nobody set.
            # TODO(sandbox-lifetime): so a forgotten box keeps its 2500m of the namespace's
            # `limits.cpu` quota until someone disposes it. Expire idle boxes instead:
            # `shutdownPolicy: Delete` with a `shutdownTime` each `exec` pushes forward, as
            # haku-console's claims do (`haku/sandbox/kubernetes_client.py`).
            "spec": {
                "podTemplate": {**pod_template, "spec": spec},
                # Carried from the template, not dropped: a Pod whose container mounts a volume the
                # Sandbox never declares is refused admission, so a template with claims cannot
                # produce a box without them.
                "volumeClaimTemplates": template["spec"].get("volumeClaimTemplates", []),
                "shutdownPolicy": "Retain",
            },
        }
        try:
            await self._custom_objects.create_namespaced_custom_object(
                *SANDBOX_API, self._binding.namespace, SANDBOXES_PLURAL, body
            )
        except ApiException as error:
            if error.status == 409:
                # Another replica stamped the same box; its object is as good as ours.
                return
            raise

    async def info(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        sandbox = await self._sandbox(caller, name)
        if sandbox is None:
            raise SandboxActionError(f"no sandbox named {name!r}; create it first")
        return await self._info(caller, sandbox)

    async def list(self, caller: ServiceAccountRef) -> list[SandboxInfo]:
        page = await self._custom_objects.list_namespaced_custom_object(
            *SANDBOX_API,
            self._binding.namespace,
            SANDBOXES_PLURAL,
            label_selector=(
                f"{MANAGED_LABEL}=true,{CALLER_LABEL}={caller.name},{CALLER_NAMESPACE_LABEL}={caller.namespace}"
            ),
        )
        items = cast(Sequence[dict[str, Any]], cast(dict[str, Any], page)["items"])
        return [await self._info(caller, sandbox) for sandbox in items]

    async def dispose(self, caller: ServiceAccountRef, name: str) -> bool:
        """Delete the caller's sandbox; False when there was none to delete."""
        if await self._sandbox(caller, name) is None:
            return False
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                *SANDBOX_API,
                self._binding.namespace,
                SANDBOXES_PLURAL,
                _object_name(caller, name),
                body=k8s_client.V1DeleteOptions(),
            )
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
        sandbox = await self._sandbox(caller, name)
        if sandbox is None:
            raise SandboxActionError(f"no sandbox named {name!r}; create it first")
        if _ready(sandbox) is None:
            unready = condition(sandbox, READY_CONDITION) or {}
            said = unready.get("message") or unready.get("reason") or "the controller has not said why"
            raise SandboxActionError(f"sandbox {name!r} is not ready ({said})")
        info = await self._info(caller, sandbox)
        if info.pod_name is None:
            # Ready but no Pod to reach: the controller has not published one yet, or the one it
            # published is gone. Distinct from not-ready, and a caller that conflates them polls
            # a box whose own controller says it is fine.
            raise SandboxActionError(f"sandbox {name!r} is ready but has no running Pod to exec into")
        return await self._exec_runner.run(
            pod_name=info.pod_name,
            namespace=self._binding.namespace,
            container=_workload_container(sandbox),
            script=script,
            cwd=cwd,
            max_output_bytes=min(max_output_bytes, self._binding.max_output_bytes),
            timeout_seconds=min(timeout_seconds, self._binding.max_timeout_seconds),
        )


@dataclass(frozen=True)
class SandboxClients:
    """This service's own Kubernetes access, from which one inventory per configured group is built.

    Held apart from the binding because the clients are the process's and the binding is reviewed
    configuration: several groups may be configured, and they share one API client between them.
    """

    custom_objects: CustomObjectsClient
    core_v1: CoreV1Api
    exec_runner: ExecRunner

    def inventory(self, binding: SandboxExecutorBinding) -> SandboxInventory:
        return SandboxInventory(
            binding, custom_objects=self.custom_objects, core_v1=self.core_v1, exec_runner=self.exec_runner
        )
