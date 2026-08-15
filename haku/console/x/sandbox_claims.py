"""The narrow declarative `SandboxClaim` one chat session runs in, and how it is inspected.

Out of `claude_chat.py` because none of it is about chat: it creates a claim, deletes it, and
turns the CR graph underneath — claim, Sandbox, Pod, runner container — into the one progress
view the SPA renders. A change to that graph should not read as a change to the turn loop.

**Reporting is best effort and says so in the data.** Provisioning is what an operator watches
when nothing is happening yet, so a step that cannot be observed reports `observation_error`
rather than raising: the answer "the claim exists and the sandbox is not visible" is worth more
than an exception that replaces the whole view.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict

from haku.console.config import ClaudeRuntimeConfig
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)

# Copying `sandbox_mcp`'s `_renew`: a few tries is enough for the resourceVersion `test` to win
# against the controller's own status writes; a persistent conflict is a bug, not contention.
_RENEW_ATTEMPTS = 3

_CLAIM_API = ("extensions.agents.x-k8s.io", "v1beta1")
_CLAIMS_PLURAL = "sandboxclaims"


def _format_shutdown_time(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProvisioningStep(StrEnum):
    CLAIM_CREATED = "claim_created"
    WAITING_FOR_SANDBOX = "waiting_for_sandbox"
    WAITING_FOR_POD = "waiting_for_pod"
    WAITING_FOR_POD_READY = "waiting_for_pod_ready"
    WAITING_FOR_RUNNER = "waiting_for_runner"


class ClaudeSandboxProvisioningView(BaseModel):
    """Non-secret Kubernetes state explaining what sandbox provisioning is waiting on."""

    model_config = ConfigDict(extra="forbid")

    step: ProvisioningStep
    inspected_at: datetime
    claim_name: str
    claim_ready: bool | None = None
    claim_reason: str | None = None
    claim_message: str | None = None
    sandbox_name: str | None = None
    sandbox_ready: bool | None = None
    pod_name: str | None = None
    pod_phase: str | None = None
    pod_ready: bool | None = None
    runner_ready: bool | None = None
    runner_state: str | None = None
    observation_error: str | None = None


@dataclass(frozen=True)
class KubernetesClients:
    """The three clients built from one in-cluster configuration.

    One object because they are one thing: they are built together, closed together, and no state
    where some exist and others do not is reachable — which is what the four `assert`s this
    replaced were restating at every call site. Public so a test can supply recorded ones through
    the constructor instead of reaching past it into private attributes.
    """

    api: ApiClient
    custom_objects: CustomObjectsClient
    core_v1: CoreV1Api


class SandboxClaims(Protocol):
    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None: ...

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None: ...

    async def delete(self, *, session_id: UUID) -> None: ...

    async def inspect(self, *, session_id: UUID) -> ClaudeSandboxProvisioningView: ...

    async def aclose(self) -> None: ...


class KubernetesSandboxClaims:
    """Create the narrow declarative SandboxClaim used by one chat session."""

    def __init__(self, config: ClaudeRuntimeConfig, clients: KubernetesClients | None = None):
        self._config = config
        self._clients = clients
        self._lock = asyncio.Lock()

    async def _connected(self) -> KubernetesClients:
        # The lock is held on every call rather than only the first: acquiring an uncontended
        # `asyncio.Lock` does not suspend, so the fast path costs nothing and there is no
        # check-then-build window to reason about.
        async with self._lock:
            if self._clients is None:
                configuration = k8s_client.Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise RuntimeError("Kubernetes in-cluster configuration is unavailable") from error
                api = ApiClient(configuration=configuration)
                self._clients = KubernetesClients(
                    api=api,
                    # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
                    custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
                    core_v1=CoreV1Api(api),
                )
            return self._clients

    def _claim_name(self, session_id: UUID) -> str:
        return f"claude-{session_id.hex}"

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {
                "name": self._claim_name(session_id),
                "labels": {
                    "app.kubernetes.io/managed-by": "haku-console",
                    "haku.allegedly.works/runtime": "claude-chat",
                },
            },
            "spec": {
                "warmPoolRef": {"name": self._config.warm_pool},
                "lifecycle": {"shutdownPolicy": "DeleteForeground", "shutdownTime": _format_shutdown_time(expires_at)},
                "env": [
                    {"name": "HAKU_CLAUDE_SESSION_ID", "value": str(session_id)},
                    {"name": "HAKU_AGENT_SDK_RUNNER_TOKEN", "value": bridge_token},
                ],
            },
        }
        client = (await self._connected()).custom_objects
        await client.create_namespaced_custom_object(*_CLAIM_API, self._config.namespace, _CLAIMS_PLURAL, body)

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None:
        """Slide this session's sandbox shutdown deadline forward while a replica is tending it.

        The deadline is a lease, not a hard timer: the console pushes it out on the same heartbeat
        that renews the session's console lease, so a conversation in full flow no longer dies at
        exactly `session_ttl_seconds`, and the controller reclaims the sandbox soon after nothing
        tends it. `test` on `resourceVersion` plus a 409 retry, copying `sandbox_mcp`'s `_renew`,
        so a concurrent writer never clobbers the deadline.

        Best effort: a slide that fails leaves the sandbox on its previous deadline and the sweep
        handles the fallout, so it must not take the renewal heartbeat down with it.
        """
        client = (await self._connected()).custom_objects
        name = self._claim_name(session_id)
        shutdown_time = _format_shutdown_time(expires_at)
        for attempt in range(_RENEW_ATTEMPTS):
            try:
                claim = await client.get_namespaced_custom_object(
                    *_CLAIM_API, self._config.namespace, _CLAIMS_PLURAL, name
                )
            except k8s_client.ApiException as error:
                if error.status != 404:
                    logger.warning("could not read sandbox claim %s to slide its deadline: %s", name, error)
                return  # a gone claim is the session ending; the lease sweep is what notices.
            patch = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": _nested_string(claim, "metadata", "resourceVersion"),
                },
                {"op": "replace", "path": "/spec/lifecycle/shutdownTime", "value": shutdown_time},
            ]
            try:
                await client.patch_namespaced_custom_object(
                    *_CLAIM_API,
                    self._config.namespace,
                    _CLAIMS_PLURAL,
                    name,
                    patch,
                    _content_type="application/json-patch+json",
                )
                return
            except k8s_client.ApiException as error:
                if error.status == 409 and attempt + 1 < _RENEW_ATTEMPTS:
                    continue
                logger.warning("could not slide sandbox deadline for %s: %s", name, error)
                return

    async def delete(self, *, session_id: UUID) -> None:
        client = (await self._connected()).custom_objects
        try:
            await client.delete_namespaced_custom_object(
                "extensions.agents.x-k8s.io",
                "v1beta1",
                self._config.namespace,
                "sandboxclaims",
                self._claim_name(session_id),
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise

    async def inspect(self, *, session_id: UUID) -> ClaudeSandboxProvisioningView:
        claim_name = self._claim_name(session_id)
        clients = await self._connected()
        try:
            claim = await clients.custom_objects.get_namespaced_custom_object(
                "extensions.agents.x-k8s.io", "v1beta1", self._config.namespace, "sandboxclaims", claim_name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return provisioning_view(claim_name, step=ProvisioningStep.CLAIM_CREATED)
            raise

        claim_condition = _condition(claim, "Ready")
        claim_reason = _condition_text(claim_condition, "reason")
        claim_message = _condition_text(claim_condition, "message")
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            return provisioning_view(
                claim_name,
                step=ProvisioningStep.WAITING_FOR_SANDBOX,
                claim_ready=_condition_bool(claim_condition),
                claim_reason=claim_reason,
                claim_message=claim_message,
            )

        try:
            sandbox = await clients.custom_objects.get_namespaced_custom_object(
                "agents.x-k8s.io", "v1beta1", self._config.namespace, "sandboxes", sandbox_name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return provisioning_view(
                    claim_name,
                    step=ProvisioningStep.WAITING_FOR_SANDBOX,
                    claim_ready=_condition_bool(claim_condition),
                    claim_reason=claim_reason,
                    claim_message=claim_message,
                    sandbox_name=sandbox_name,
                )
            raise

        sandbox_condition = _condition(sandbox, "Ready")
        annotations = sandbox.get("metadata", {}).get("annotations", {}) or {}
        pod_name = str(annotations.get("agents.x-k8s.io/pod-name") or sandbox_name)
        try:
            pod = await clients.core_v1.read_namespaced_pod(pod_name, self._config.namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return provisioning_view(
                    claim_name,
                    step=ProvisioningStep.WAITING_FOR_POD,
                    claim_ready=_condition_bool(claim_condition),
                    claim_reason=claim_reason,
                    claim_message=claim_message,
                    sandbox_name=sandbox_name,
                    sandbox_ready=_condition_bool(sandbox_condition),
                    pod_name=pod_name,
                )
            raise

        pod_phase = pod.status.phase if pod.status is not None else None
        pod_ready = _pod_ready(pod)
        runner_ready, runner_state = _container_status(pod, "runner")
        step = (
            ProvisioningStep.WAITING_FOR_RUNNER
            if pod_ready and runner_ready
            else ProvisioningStep.WAITING_FOR_POD_READY
        )
        return provisioning_view(
            claim_name,
            step=step,
            claim_ready=_condition_bool(claim_condition),
            claim_reason=claim_reason,
            claim_message=claim_message,
            sandbox_name=sandbox_name,
            sandbox_ready=_condition_bool(sandbox_condition),
            pod_name=pod_name,
            pod_phase=pod_phase,
            pod_ready=pod_ready,
            runner_ready=runner_ready,
            runner_state=runner_state,
        )

    async def aclose(self) -> None:
        if self._clients is not None:
            await self._clients.api.close()
            self._clients = None


def provisioning_view(claim_name: str, *, step: ProvisioningStep, **values: Any) -> ClaudeSandboxProvisioningView:
    return ClaudeSandboxProvisioningView(claim_name=claim_name, step=step, inspected_at=datetime.now(UTC), **values)


def _condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    conditions = resource.get("status", {}).get("conditions", []) or []
    return next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == condition_type
        ),
        None,
    )


def _condition_text(condition: dict[str, Any] | None, key: str) -> str | None:
    if condition is None:
        return None
    value = condition.get(key)
    return value if isinstance(value, str) and value else None


def _condition_bool(condition: dict[str, Any] | None) -> bool | None:
    status = _condition_text(condition, "status")
    if status == "True":
        return True
    if status == "False":
        return False
    return None


def _nested_string(resource: dict[str, Any], *path: str) -> str | None:
    value: Any = resource
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value else None


def _pod_ready(pod: k8s_client.V1Pod) -> bool | None:
    if pod.status is None or pod.status.conditions is None:
        return None
    condition = next((item for item in pod.status.conditions if item.type == "Ready"), None)
    if condition is None:
        return None
    if condition.status == "True":
        return True
    if condition.status == "False":
        return False
    return None


def _container_status(pod: k8s_client.V1Pod, name: str) -> tuple[bool | None, str | None]:
    if pod.status is None or pod.status.container_statuses is None:
        return None, None
    status = next((item for item in pod.status.container_statuses if item.name == name), None)
    if status is None:
        return None, None
    state = status.state
    if state is None:
        return status.ready, None
    if state.running is not None:
        detail = "running"
    elif state.waiting is not None:
        reason = state.waiting.reason or "unknown"
        detail = f"waiting: {reason}"
    elif state.terminated is not None:
        reason = state.terminated.reason or f"exit {state.terminated.exit_code}"
        detail = f"terminated: {reason}"
    else:
        detail = None
    return status.ready, detail
