"""The narrow declarative `SandboxClaim` one session runs in, and how it is inspected.

Creates a claim, deletes it, and turns the CR graph underneath — claim, Sandbox, Pod, runner
container — into the one progress view the SPA renders.

**Reporting is best effort and says so in the data.** A step that cannot be observed reports
`observation_error` rather than raising: "the claim exists and the sandbox is not visible" is worth
more than an exception that replaces the whole view.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from kubernetes_asyncio import client as k8s_client, config as k8s_config, watch as k8s_watch
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict

from haku.runner.backend import LEGACY_SESSION_TOKEN_VARIABLE, SESSION_TOKEN_VARIABLE
from haku.sandbox.claims import (
    CLAIM_API_VERSION,
    CLAIM_GROUP,
    CLAIMS_PLURAL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SandboxAllocationSpec as SharedSandboxAllocationSpec,
    SandboxClaimClient,
)
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)

# Copying `haku/sandbox`'s `_renew`: a few tries is enough for the resourceVersion `test` to win
# against the controller's own status writes; a persistent conflict is a bug, not contention.
_RENEW_ATTEMPTS = 3


class ProvisioningStep(StrEnum):
    # Kubernetes does not have this session's claim: it was never created, or it has been reclaimed
    # (`session_runtime._cleanup_terminal_claim` deletes it once the session ends). Distinct from
    # `CLAIM_CREATED`, which is what the console knows when it created a claim and could not observe
    # past it.
    CLAIM_ABSENT = "claim_absent"
    CLAIM_CREATED = "claim_created"
    WAITING_FOR_SANDBOX = "waiting_for_sandbox"
    WAITING_FOR_POD = "waiting_for_pod"
    WAITING_FOR_POD_READY = "waiting_for_pod_ready"
    WAITING_FOR_RUNNER = "waiting_for_runner"


class SandboxProvisioningView(BaseModel):
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
    """The API clients and shared claim client built from one configuration.

    One object because they are built together and closed together, so no state where some exist
    and others do not is reachable. Public so a test can supply recorded ones through the
    constructor instead of reaching past it into private attributes.
    """

    api: ApiClient
    custom_objects: CustomObjectsClient
    core_v1: CoreV1Api
    claims: SandboxClaimClient


@dataclass(frozen=True, slots=True)
class SandboxClaimSpec:
    """Generic desired state for one harness's session claims.

    The claim implementation knows Kubernetes and the shared runner bootstrap only. Which native
    harness image/pool is selected and how claims are labelled is deploy-time harness composition.
    """

    namespace: str
    warm_pool: str
    claim_prefix: str
    harness_label: str
    runner_environment: Mapping[str, str]


class SandboxClaims(Protocol):
    async def create(self, *, session_id: UUID, session_token: str, expires_at: datetime) -> None: ...

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None: ...

    async def delete(self, *, session_id: UUID) -> None: ...

    async def inspect(self, *, session_id: UUID) -> SandboxProvisioningView: ...

    def observation_error(self, *, session_id: UUID, error: str) -> SandboxProvisioningView: ...

    def watch_changes(self, stop: asyncio.Event) -> AsyncIterator[None]: ...

    async def aclose(self) -> None: ...


class KubernetesSandboxClaims:
    """Create the narrow declarative SandboxClaim used by one session."""

    def __init__(self, spec: SandboxClaimSpec, clients: KubernetesClients | None = None):
        self._spec = spec
        self._clients = clients
        self._lock = asyncio.Lock()

    async def _connected(self) -> KubernetesClients:
        # Held on every call rather than only the first: acquiring an uncontended `asyncio.Lock`
        # does not suspend, so the fast path costs nothing and there is no check-then-build window.
        async with self._lock:
            if self._clients is None:
                configuration = k8s_client.Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise RuntimeError("Kubernetes in-cluster configuration is unavailable") from error
                api = ApiClient(configuration=configuration)
                custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
                core_v1 = CoreV1Api(api)
                self._clients = KubernetesClients(
                    api=api,
                    custom_objects=custom_objects,
                    core_v1=core_v1,
                    claims=SandboxClaimClient(custom_objects, core_v1, self._spec.namespace),
                )
            return self._clients

    def _claim_name(self, session_id: UUID) -> str:
        return f"{self._spec.claim_prefix}-{session_id.hex}"

    async def create(self, *, session_id: UUID, session_token: str, expires_at: datetime) -> None:
        env = {
            **self._spec.runner_environment,
            "HAKU_RUNNER_SESSION_ID": str(session_id),
            SESSION_TOKEN_VARIABLE: session_token,
            # CLEANUP(added 2026-08-29): dual mint while runner images that read only the
            # legacy name may still serve claims; drop with the fallback in
            # haku/runner/backend.py once the deployed runner image reads
            # HAKU_SESSION_TOKEN — one release after both images converge.
            LEGACY_SESSION_TOKEN_VARIABLE: session_token,
        }
        clients = await self._connected()
        await clients.claims.create(
            SharedSandboxAllocationSpec(
                namespace=self._spec.namespace,
                name=self._claim_name(session_id),
                warm_pool=self._spec.warm_pool,
                labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, "haku.allegedly.works/harness": self._spec.harness_label},
                annotations={},
                shutdown_policy="DeleteForeground",
                shutdown_time=expires_at,
                env=env,
            )
        )

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None:
        """Slide this session's sandbox shutdown deadline forward while a replica is tending it.

        The deadline is a lease, not a hard timer: the console pushes it out on the same heartbeat
        that renews the session's console lease, so a conversation in full flow does not die at
        `session_ttl_seconds` and the controller reclaims the sandbox soon after nothing tends it.
        `test` on `resourceVersion` plus a 409 retry, so a concurrent writer never clobbers it.

        Best effort: a slide that fails leaves the sandbox on its previous deadline and the sweep
        handles the fallout, so it must not take the renewal heartbeat down with it.
        """
        clients = await self._connected()
        name = self._claim_name(session_id)
        try:
            await clients.claims.renew(name, expires_at, attempts=_RENEW_ATTEMPTS)
        except k8s_client.ApiException as error:
            logger.warning("could not slide sandbox deadline for %s: %s", name, error)
        except ValueError as error:
            logger.warning("could not slide sandbox deadline for %s: %s", name, error)

    async def delete(self, *, session_id: UUID) -> None:
        clients = await self._connected()
        await clients.claims.delete(self._claim_name(session_id))

    async def inspect(self, *, session_id: UUID) -> SandboxProvisioningView:
        claim_name = self._claim_name(session_id)
        clients = await self._connected()
        try:
            graph = await clients.claims.graph(claim_name)
        except k8s_client.ApiException:
            raise
        claim = graph.claim
        if claim is None:
            return provisioning_view(claim_name, step=ProvisioningStep.CLAIM_ABSENT)

        claim_condition = _condition(claim, "Ready")
        claim_reason = _condition_text(claim_condition, "reason")
        claim_message = _condition_text(claim_condition, "message")
        sandbox_name = graph.sandbox_name
        if sandbox_name is None:
            return provisioning_view(
                claim_name,
                step=ProvisioningStep.WAITING_FOR_SANDBOX,
                claim_ready=_condition_bool(claim_condition),
                claim_reason=claim_reason,
                claim_message=claim_message,
            )

        sandbox = graph.sandbox
        if sandbox is None:
            return provisioning_view(
                claim_name,
                step=ProvisioningStep.WAITING_FOR_SANDBOX,
                claim_ready=_condition_bool(claim_condition),
                claim_reason=claim_reason,
                claim_message=claim_message,
                sandbox_name=sandbox_name,
            )

        sandbox_condition = _condition(sandbox, "Ready")
        pod_name = graph.pod_name
        pod = graph.pod
        if pod is None:
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

    def watch_changes(self, stop: asyncio.Event) -> AsyncIterator[None]:
        """Yield invalidations for claims and the resources beneath them.

        The observer deliberately receives no manifests. It only needs a wake to invalidate the
        short-lived projection cache; the next MCP read performs the authoritative, scoped graph
        inspection through ``inspect``.
        """

        async def stream() -> AsyncIterator[None]:
            clients = await self._connected()
            queue: asyncio.Queue[None] = asyncio.Queue()

            async def watch_source(method: Any, **kwargs: Any) -> None:
                while not stop.is_set():
                    watcher = k8s_watch.Watch()
                    try:
                        async for _event in watcher.stream(method, **kwargs, timeout_seconds=300):
                            if stop.is_set():
                                return
                            await queue.put(None)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("Kubernetes sandbox watch failed; retrying", exc_info=True)
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), timeout=1)
                    finally:
                        watcher.stop()

            tasks = [
                asyncio.create_task(
                    watch_source(
                        clients.custom_objects.list_namespaced_custom_object,
                        group=CLAIM_GROUP,
                        version=CLAIM_API_VERSION,
                        namespace=self._spec.namespace,
                        plural=CLAIMS_PLURAL,
                        label_selector=(
                            f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},"
                            f"haku.allegedly.works/harness={self._spec.harness_label}"
                        ),
                    ),
                    name=f"sandbox-claim-watch-{self._spec.harness_label}",
                ),
                asyncio.create_task(
                    watch_source(
                        clients.custom_objects.list_namespaced_custom_object,
                        group="agents.x-k8s.io",
                        version="v1beta1",
                        namespace=self._spec.namespace,
                        plural="sandboxes",
                    ),
                    name=f"sandbox-watch-{self._spec.harness_label}",
                ),
                asyncio.create_task(
                    watch_source(clients.core_v1.list_namespaced_pod, namespace=self._spec.namespace),
                    name=f"sandbox-pod-watch-{self._spec.harness_label}",
                ),
            ]
            try:
                while not stop.is_set():
                    get_event = asyncio.create_task(queue.get())
                    stop_wait = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait((get_event, stop_wait), return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if stop_wait in done:
                        return
                    if get_event in done:
                        yield None
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        return stream()

    def observation_error(self, *, session_id: UUID, error: str) -> SandboxProvisioningView:
        return provisioning_view(
            self._claim_name(session_id), step=ProvisioningStep.CLAIM_CREATED, observation_error=error
        )


def provisioning_view(claim_name: str, *, step: ProvisioningStep, **values: Any) -> SandboxProvisioningView:
    return SandboxProvisioningView(claim_name=claim_name, step=step, inspected_at=datetime.now(UTC), **values)


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
