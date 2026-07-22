"""Kubernetes implementation of Haku's semantic agent-sandbox tools.

The public handle is the ``SandboxClaim`` name. Claims use ``Retain`` so expiry removes the
resource-consuming Sandbox/Pod while leaving an inspectable ``ClaimExpired`` tombstone. Commands
run through Kubernetes' exec WebSocket with both an in-pod coreutils timeout and a client-side
transport deadline; the latter prevents a broken stream from occupying a console worker forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiohttp import WSMsgType
from fastmcp.exceptions import ToolError
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, ApiException, Configuration, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDOUT_CHANNEL

from haku.console.config import AgentSandboxConfig
from haku.console.tools.sandbox import SandboxExecResult, SandboxInfo
from mcp_infra.exec.models import BaseExecResult, Exited, ExecStream, TimedOut, TruncatedStream, async_timer

logger = logging.getLogger(__name__)

CLAIM_GROUP = "extensions.agents.x-k8s.io"
SANDBOX_GROUP = "agents.x-k8s.io"
API_VERSION = "v1beta1"
CLAIMS_PLURAL = "sandboxclaims"
SANDBOXES_PLURAL = "sandboxes"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "haku-console-sandbox-mcp"
POOL_LABEL = "haku.allegedly.works/sandbox-pool"
HANDLE_PREFIX = "hs-"
CLAIM_EXPIRED_REASON = "ClaimExpired"

_FATAL_ALLOCATION_REASONS = frozenset(
    {
        "EnvVarsInjectionRejected",
        "InvalidMetadata",
        "ReconcilerError",
        "TemplateNotFound",
        "VolumeClaimTemplatesError",
        "WarmPoolNotFound",
    }
)


class ExecRunner(Protocol):
    async def run(
        self, *, pod_name: str, namespace: str, container: str, cmd: list[str], max_bytes: int, timeout_ms: int
    ) -> BaseExecResult: ...


@dataclass(slots=True)
class _Capture:
    limit: int
    stored: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self.limit - len(self.stored)
        if remaining > 0:
            self.stored.extend(chunk[:remaining])

    def render(self) -> ExecStream:
        if self.limit <= 0:
            return ""
        text = bytes(self.stored).decode("utf-8", errors="replace")
        if self.total_bytes > len(self.stored):
            return TruncatedStream(truncated_text=text, total_bytes=self.total_bytes)
        return text


class KubernetesWebSocketExecRunner:
    """Runs one bounded, non-interactive Kubernetes pod exec."""

    def __init__(self, configuration: Configuration) -> None:
        self._configuration = configuration

    async def run(
        self, *, pod_name: str, namespace: str, container: str, cmd: list[str], max_bytes: int, timeout_ms: int
    ) -> BaseExecResult:
        stdout = _Capture(max_bytes)
        stderr = _Capture(max_bytes)
        error_data = bytearray()
        # The haku-state template contract requires GNU coreutils `timeout`. Keeping the timeout
        # inside the pod means closing a stuck client connection does not leave the command running.
        wrapped_cmd = [
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{timeout_ms / 1000:.3f}s",
            *cmd,
        ]

        async with async_timer() as duration_ms:
            try:
                # The extra 15 seconds lets coreutils deliver its TERM/KILL result while still
                # bounding a wedged handshake/apiserver/WebSocket beyond the caller's five-minute
                # cap. A fresh WebSocket ApiClient is required for each exec and closes immediately;
                # the ordinary REST client remains reusable for claim/pod reads.
                async with asyncio.timeout(timeout_ms / 1000 + 15):
                    async with WsApiClient(configuration=self._configuration) as ws_api:
                        core_v1 = k8s_client.CoreV1Api(api_client=ws_api)
                        websocket = await core_v1.connect_get_namespaced_pod_exec(
                            pod_name,
                            namespace,
                            command=wrapped_cmd,
                            container=container,
                            stderr=True,
                            stdin=False,
                            stdout=True,
                            tty=False,
                            _preload_content=False,
                        )
                        async with websocket as ws:
                            while True:
                                message = await ws.receive()
                                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                                    break
                                if message.type == WSMsgType.ERROR:
                                    raise ToolError(f"Kubernetes exec WebSocket failed: {ws.exception()}")
                                if message.type not in {WSMsgType.BINARY, WSMsgType.TEXT}:
                                    continue
                                payload = message.data.encode() if isinstance(message.data, str) else message.data
                                if not payload:
                                    continue
                                channel, chunk = payload[0], payload[1:]
                                if channel == STDOUT_CHANNEL:
                                    stdout.append(chunk)
                                elif channel == STDERR_CHANNEL:
                                    stderr.append(chunk)
                                elif channel == ERROR_CHANNEL:
                                    # Kubernetes' status frame is tiny. Bound malformed input anyway.
                                    remaining = 64 * 1024 - len(error_data)
                                    if remaining > 0:
                                        error_data.extend(chunk[:remaining])
            except TimeoutError:
                return BaseExecResult(
                    exit=TimedOut(), stdout=stdout.render(), stderr=stderr.render(), duration_ms=duration_ms()
                )
            except ApiException as error:
                raise ToolError(_api_error("exec command", error)) from error

            if not error_data:
                raise ToolError("Kubernetes exec ended without a command status frame")
            try:
                exit_code = WsApiClient.parse_error_data(bytes(error_data))
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError("Kubernetes exec returned a malformed command status frame") from error
            exit_status = TimedOut() if exit_code == 124 else Exited(exit_code=exit_code)
            return BaseExecResult(
                exit=exit_status, stdout=stdout.render(), stderr=stderr.render(), duration_ms=duration_ms()
            )


class KubernetesAgentSandboxClient:
    """Claim lifecycle and health orchestration over injected Kubernetes API clients."""

    def __init__(
        self,
        settings: AgentSandboxConfig,
        *,
        api_client: ApiClient,
        custom_objects: CustomObjectsApi,
        core_v1: CoreV1Api,
        exec_runner: ExecRunner,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._api_client = api_client
        self._custom_objects = custom_objects
        self._core_v1 = core_v1
        self._exec_runner = exec_runner
        self._now = now or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        await self._api_client.close()

    async def reserve(self) -> str:
        expires_at = self._now() + timedelta(seconds=self._settings.lease_seconds)
        body = {
            "apiVersion": f"{CLAIM_GROUP}/{API_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {
                "generateName": HANDLE_PREFIX,
                "labels": {
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    POOL_LABEL: self._settings.warm_pool,
                },
            },
            "spec": {
                "warmPoolRef": {"name": self._settings.warm_pool},
                "lifecycle": {
                    # Expiry releases the costly Pod/Sandbox but leaves the claim as an observable
                    # tombstone. The namespaced janitor deletes that tombstone seven days later.
                    "shutdownPolicy": "Retain",
                    "shutdownTime": _format_timestamp(expires_at),
                },
            },
        }
        try:
            claim = await self._custom_objects.create_namespaced_custom_object(
                CLAIM_GROUP,
                API_VERSION,
                self._settings.namespace,
                CLAIMS_PLURAL,
                body,
            )
        except ApiException as error:
            raise ToolError(_api_error("reserve sandbox", error)) from error
        handle = str(claim.get("metadata", {}).get("name", ""))
        if not _valid_handle(handle):
            raise ToolError("Kubernetes created a SandboxClaim without a valid Haku handle")

        last_info: SandboxInfo | None = None
        try:
            async with asyncio.timeout(self._settings.reserve_timeout_seconds):
                while True:
                    last_info = await self.info(handle)
                    if last_info.state == "ready":
                        logger.info("reserved Haku sandbox handle=%s pod=%s", handle, last_info.pod_name)
                        return handle
                    if last_info.state in {"expired", "not_found"}:
                        raise ToolError(f"sandbox {handle} became {last_info.state} while being reserved")
                    if last_info.reason in _FATAL_ALLOCATION_REASONS:
                        detail = f"{last_info.reason}: {last_info.message or ''}".rstrip()
                        raise ToolError(f"sandbox {handle} allocation failed: {detail}")
                    await asyncio.sleep(self._settings.poll_interval_seconds)
        except TimeoutError as error:
            await self._delete_failed_reservation(handle)
            detail = f" (last state: {last_info.state}/{last_info.reason})" if last_info is not None else ""
            raise ToolError(
                f"sandbox {handle} was not ready within {self._settings.reserve_timeout_seconds}s{detail}"
            ) from error
        except ToolError:
            await self._delete_failed_reservation(handle)
            raise

    async def execute(
        self, *, handle: str, cmd: list[str], max_bytes: int, timeout_ms: int
    ) -> SandboxExecResult:
        # Renew first so a command started near the old deadline cannot lose its pod mid-exec.
        await self._renew(handle)
        state = await self.info(handle)
        if state.state != "ready" or state.pod_name is None:
            raise ToolError(
                f"sandbox {handle} is not ready (state={state.state}, reason={state.reason or 'unknown'}); call info"
            )
        result = await self._exec_runner.run(
            pod_name=state.pod_name,
            namespace=self._settings.namespace,
            container=self._settings.container,
            cmd=cmd,
            max_bytes=max_bytes,
            timeout_ms=timeout_ms,
        )
        # Make the idle window start at completion rather than at command start.
        expires_at = await self._renew(handle)
        return SandboxExecResult.model_validate({**result.model_dump(), "expires_at": expires_at})

    async def info(self, handle: str) -> SandboxInfo:
        claim = await self._get_custom(CLAIM_GROUP, CLAIMS_PLURAL, handle, action="inspect sandbox claim")
        if claim is None or not self._is_managed_claim(claim):
            return SandboxInfo(handle=handle, state="not_found", healthy=False, expires_at=None)

        expires_at = _claim_expiry(claim)
        ready = _condition(claim, "Ready")
        reason = str(ready.get("reason")) if ready and ready.get("reason") else None
        message = str(ready.get("message")) if ready and ready.get("message") else None
        if reason == CLAIM_EXPIRED_REASON or (expires_at is not None and expires_at <= self._now()):
            return SandboxInfo(
                handle=handle,
                state="expired",
                healthy=False,
                expires_at=expires_at,
                reason=CLAIM_EXPIRED_REASON,
                message=message or "The sliding lease expired; the claim is retained for inspection.",
            )

        sandbox_name = str(claim.get("status", {}).get("sandbox", {}).get("name", "")) or None
        claim_ready = ready is not None and ready.get("status") == "True"
        if sandbox_name is None:
            state = "unhealthy" if reason in _FATAL_ALLOCATION_REASONS else "allocating"
            return SandboxInfo(
                handle=handle,
                state=state,
                healthy=False,
                expires_at=expires_at,
                reason=reason,
                message=message,
            )

        sandbox = await self._get_custom(SANDBOX_GROUP, SANDBOXES_PLURAL, sandbox_name, action="inspect sandbox")
        if sandbox is None:
            return SandboxInfo(
                handle=handle,
                state="unhealthy" if claim_ready else "allocating",
                healthy=False,
                expires_at=expires_at,
                sandbox_name=sandbox_name,
                reason=reason or "SandboxMissing",
                message=message or "The adopted Sandbox object is missing.",
            )
        annotations = sandbox.get("metadata", {}).get("annotations", {}) or {}
        pod_name = str(annotations.get(POD_NAME_ANNOTATION) or sandbox_name)
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, self._settings.namespace)
        except ApiException as error:
            if error.status == 404:
                return SandboxInfo(
                    handle=handle,
                    state="unhealthy" if claim_ready else "allocating",
                    healthy=False,
                    expires_at=expires_at,
                    sandbox_name=sandbox_name,
                    pod_name=pod_name,
                    reason=reason or "PodMissing",
                    message=message or "The Sandbox pod is missing.",
                )
            raise ToolError(_api_error("inspect sandbox pod", error)) from error

        sandbox_ready = _condition(sandbox, "Ready")
        pod_ready = _pod_is_ready(pod, self._settings.container)
        healthy = claim_ready and sandbox_ready is not None and sandbox_ready.get("status") == "True" and pod_ready
        if healthy:
            return SandboxInfo(
                handle=handle,
                state="ready",
                healthy=True,
                expires_at=expires_at,
                sandbox_name=sandbox_name,
                pod_name=pod_name,
                reason=reason,
                message=message,
            )
        pod_phase = str(getattr(getattr(pod, "status", None), "phase", "") or "")
        sandbox_reason = (
            str(sandbox_ready.get("reason")) if sandbox_ready is not None and sandbox_ready.get("reason") else None
        )
        return SandboxInfo(
            handle=handle,
            state="allocating" if pod_phase == "Pending" and not claim_ready else "unhealthy",
            healthy=False,
            expires_at=expires_at,
            sandbox_name=sandbox_name,
            pod_name=pod_name,
            reason=reason or sandbox_reason or "PodNotReady",
            message=message or f"Sandbox pod phase is {pod_phase or 'unknown'}.",
        )

    async def _renew(self, handle: str) -> datetime:
        claim = await self._get_custom(CLAIM_GROUP, CLAIMS_PLURAL, handle, action="renew sandbox claim")
        if claim is None or not self._is_managed_claim(claim):
            raise ToolError(f"sandbox handle {handle!r} was not found")
        current = _claim_expiry(claim)
        now = self._now()
        if current is None:
            raise ToolError(f"sandbox {handle} has no lease deadline")
        ready = _condition(claim, "Ready")
        if current <= now or (ready is not None and ready.get("reason") == CLAIM_EXPIRED_REASON):
            raise ToolError(f"sandbox {handle} has expired; reserve a new sandbox")
        target = max(current, now + timedelta(seconds=self._settings.lease_seconds))
        try:
            await self._custom_objects.patch_namespaced_custom_object(
                CLAIM_GROUP,
                API_VERSION,
                self._settings.namespace,
                CLAIMS_PLURAL,
                handle,
                {"spec": {"lifecycle": {"shutdownTime": _format_timestamp(target)}}},
                _content_type="application/merge-patch+json",
            )
        except ApiException as error:
            raise ToolError(_api_error("renew sandbox claim", error)) from error
        return target

    async def _get_custom(self, group: str, plural: str, name: str, *, action: str) -> dict[str, Any] | None:
        try:
            value = await self._custom_objects.get_namespaced_custom_object(
                group,
                API_VERSION,
                self._settings.namespace,
                plural,
                name,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise ToolError(_api_error(action, error)) from error
        return value

    def _is_managed_claim(self, claim: dict[str, Any]) -> bool:
        metadata = claim.get("metadata", {})
        labels = metadata.get("labels", {}) or {}
        pool = claim.get("spec", {}).get("warmPoolRef", {}).get("name")
        return (
            labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
            and labels.get(POOL_LABEL) == self._settings.warm_pool
            and pool == self._settings.warm_pool
        )

    async def _delete_failed_reservation(self, handle: str) -> None:
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                CLAIM_GROUP,
                API_VERSION,
                self._settings.namespace,
                CLAIMS_PLURAL,
                handle,
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as error:
            if error.status != 404:
                logger.warning("failed to clean up unsuccessful sandbox reservation %s: %s", handle, error)


class InClusterAgentSandboxClient:
    """Lazily creates the Kubernetes clients once an async tool call has a running loop."""

    def __init__(self, settings: AgentSandboxConfig) -> None:
        self._settings = settings
        self._client: KubernetesAgentSandboxClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> KubernetesAgentSandboxClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                configuration = Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise ToolError("Kubernetes in-cluster configuration is unavailable") from error
                api_client = ApiClient(configuration=configuration)
                self._client = KubernetesAgentSandboxClient(
                    self._settings,
                    api_client=api_client,
                    custom_objects=CustomObjectsApi(api_client),
                    core_v1=CoreV1Api(api_client),
                    exec_runner=KubernetesWebSocketExecRunner(configuration),
                )
        return self._client

    async def reserve(self) -> str:
        return await (await self._get_client()).reserve()

    async def execute(
        self, *, handle: str, cmd: list[str], max_bytes: int, timeout_ms: int
    ) -> SandboxExecResult:
        return await (await self._get_client()).execute(
            handle=handle, cmd=cmd, max_bytes=max_bytes, timeout_ms=timeout_ms
        )

    async def info(self, handle: str) -> SandboxInfo:
        return await (await self._get_client()).info(handle)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    for condition in resource.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == condition_type:
            return condition
    return None


def _claim_expiry(claim: dict[str, Any]) -> datetime | None:
    raw = claim.get("spec", {}).get("lifecycle", {}).get("shutdownTime")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError("sandbox claim has an invalid lease deadline") from error
    if value.tzinfo is None:
        raise ToolError("sandbox claim lease deadline has no timezone")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_handle(handle: str) -> bool:
    suffix = handle.removeprefix(HANDLE_PREFIX)
    return (
        handle.startswith(HANDLE_PREFIX)
        and bool(suffix)
        and len(handle) <= 63
        and suffix.isalnum()
        and suffix == suffix.lower()
    )


def _pod_is_ready(pod: Any, container: str) -> bool:
    status = getattr(pod, "status", None)
    if status is None or status.phase != "Running":
        return False
    for container_status in status.container_statuses or []:
        if container_status.name == container:
            return bool(container_status.ready)
    return False


def _api_error(action: str, error: ApiException) -> str:
    reason = f": {error.reason}" if error.reason else ""
    return f"Kubernetes could not {action} (HTTP {error.status or 'unknown'}{reason})"
