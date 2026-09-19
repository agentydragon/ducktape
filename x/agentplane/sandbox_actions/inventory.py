"""Sandboxes this surface stamps, reads and deletes, and the commands it runs in them.

Its own inventory rather than the integration app's (<../app/inventory.py>): that one mints a
ServiceAccount per Sandbox and owns it, stamps the app's `managed` label, and is read back by the
app's fleet view and ingestion coordinator, which would then look for a runner these boxes do not
have. What is shared is the CRD, not the code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiException, CoreV1Api

from mcp_infra.exec.kubernetes import CommandResult, ExecRunner
from util.agent_sandbox import EXTENSIONS_API, SANDBOX_API, SANDBOXES_PLURAL, TEMPLATES_PLURAL, condition, pod_name
from util.kubernetes import CustomObjectsClient
from x.agentplane.sandbox_actions.binding import SandboxEnvironment, SandboxExecutorBinding
from x.agentplane.sandbox_actions.models import SandboxInfo, SandboxState
from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

_PREFIX = "sandbox-actions.agentplane.allegedly.works"
# What this surface will touch. The Action Service's `pods/exec` grant is namespace-wide and cannot
# express "only the boxes this Action made", so the boundary is here: every read, exec and delete
# selects on this label and on the caller's, and a Sandbox carrying neither is not ours to act on.
MANAGED_LABEL = f"{_PREFIX}/managed"
CALLER_LABEL = f"{_PREFIX}/caller"
CALLER_NAMESPACE_LABEL = f"{_PREFIX}/caller-namespace"
ENVIRONMENT_LABEL = f"{_PREFIX}/environment"
NAME_LABEL = f"{_PREFIX}/name"

_POLL_SECONDS = 2.0


class SandboxActionError(Exception):
    """The request cannot be carried out, in terms the calling agent can act on."""


class ForeignSandboxError(SandboxActionError):
    """A Sandbox of that name exists and belongs to someone else, or to nothing this surface made."""


def _labels(caller: ServiceAccountRef, name: str, environment: str) -> dict[str, str]:
    return {
        MANAGED_LABEL: "true",
        CALLER_LABEL: caller.name,
        CALLER_NAMESPACE_LABEL: caller.namespace,
        ENVIRONMENT_LABEL: environment,
        NAME_LABEL: name,
    }


def _object_name(caller: ServiceAccountRef, name: str) -> str:
    """Deterministic, so provisioning the same name twice reaches the same box rather than a second one."""
    return f"{caller.name}-{name}"[:63].rstrip("-")


def _state(sandbox: dict[str, Any]) -> tuple[SandboxState, str | None]:
    """The controller's own Ready condition, not a second opinion derived from the Pod.

    The Agent Sandbox controller owns this lifecycle and publishes its verdict; re-deciding
    readiness here from Pod phase and container statuses would be a weaker copy that can disagree
    with the authority, and its reasons would be poorer than the ones the controller writes. So the
    reason is passed through verbatim rather than re-worded: `WarmPoolNotFound` tells its reader
    what to do, where "pod phase Pending" does not.
    """
    ready = condition(sandbox, "Ready")
    if ready is None:
        return SandboxState.NOT_READY, None
    if ready.get("status") == "True":
        return SandboxState.READY, None
    return SandboxState.NOT_READY, ready.get("message") or ready.get("reason")


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

    def environment(self, name: str | None) -> tuple[str, SandboxEnvironment]:
        key = name or self._binding.default_environment
        environment = self._binding.environments.get(key)
        if environment is None:
            offered = ", ".join(sorted(self._binding.environments))
            raise SandboxActionError(f"unknown environment {key!r}; this deployment offers {offered}")
        return key, environment

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
        state, reason = _state(sandbox)
        return SandboxInfo(
            name=metadata["labels"][NAME_LABEL],
            state=state,
            environment=metadata["labels"][ENVIRONMENT_LABEL],
            created_at=metadata.get("creationTimestamp"),
            # Only for the name to exec into; readiness is the controller's answer above.
            pod_name=await self._pod_name(sandbox) if state is SandboxState.READY else None,
            reason=reason,
        )

    async def provision(self, caller: ServiceAccountRef, name: str, environment_name: str | None) -> SandboxInfo:
        """Create the caller's sandbox if it has none by that name, then wait for it to come up.

        Idempotent: an existing sandbox of that name is returned as it stands, whatever environment
        it was created from. Deciding that the shape is wrong is the caller's, and `dispose` is how
        it acts on that.
        """
        key, environment = self.environment(environment_name)
        if (existing := await self._sandbox(caller, name)) is None:
            await self._create(caller, name, key, environment)
        elif existing["metadata"]["labels"][ENVIRONMENT_LABEL] != key:
            logger.info("sandbox %r exists from environment %r; returning it as it stands", name, key)
        return await self._await_ready(caller, name)

    async def _create(self, caller: ServiceAccountRef, name: str, key: str, environment: SandboxEnvironment) -> None:
        template = cast(
            dict[str, Any],
            await self._custom_objects.get_namespaced_custom_object(
                *EXTENSIONS_API, self._binding.namespace, TEMPLATES_PLURAL, environment.template
            ),
        )
        pod_template = cast(dict[str, Any], template["spec"]["podTemplate"])
        spec = {**cast(dict[str, Any], pod_template.get("spec", {})), "serviceAccountName": caller.name}
        body = {
            "apiVersion": SANDBOX_API.api_version,
            "kind": "Sandbox",
            "metadata": {"name": _object_name(caller, name), "labels": _labels(caller, name, key)},
            # Retain: this surface owns deletion, and a box whose caller is still working in it must
            # not be collected out from under them on a schedule nobody set.
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
                # Another replica admitted the same provision; its object is as good as ours.
                return
            raise

    async def _await_ready(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        """Poll until the box is usable or the budget runs out; a timeout is reported, not raised.

        Not-ready with the controller's reason is a truthful answer an agent can poll on, where an
        exception would lose the sandbox it just created.
        """
        deadline = datetime.now(UTC).timestamp() + self._binding.provisioning_timeout_seconds
        while True:
            sandbox = await self._sandbox(caller, name)
            if sandbox is None:
                raise SandboxActionError(f"sandbox {name!r} disappeared while coming up")
            info = await self._info(caller, sandbox)
            if info.state is not SandboxState.NOT_READY or datetime.now(UTC).timestamp() >= deadline:
                return info
            await asyncio.sleep(_POLL_SECONDS)

    async def info(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        sandbox = await self._sandbox(caller, name)
        if sandbox is None:
            raise SandboxActionError(f"no sandbox named {name!r}; provision it first")
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
            raise SandboxActionError(f"no sandbox named {name!r}; provision it first")
        info = await self._info(caller, sandbox)
        if info.state is not SandboxState.READY:
            raise SandboxActionError(
                f"sandbox {name!r} is not ready ({info.reason or 'the controller gives no reason'})"
            )
        if info.pod_name is None:
            # Ready but no Pod to reach: the controller has not published one yet, or the one it
            # published is gone. Distinct from not-ready, and a caller that conflates them polls
            # a box whose own controller says it is fine.
            raise SandboxActionError(f"sandbox {name!r} is ready but has no running Pod to exec into")
        _, environment = self.environment(info.environment)
        return await self._exec_runner.run(
            pod_name=info.pod_name,
            namespace=self._binding.namespace,
            container=environment.container,
            script=script,
            cwd=cwd or environment.default_cwd,
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
