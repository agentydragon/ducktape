"""Kubernetes implementation over the external Agent Sandbox CRDs."""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, TypeGuard, cast

from aiohttp import WSMessage, WSMsgType
from fastmcp.exceptions import ToolError
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, ApiException, Configuration, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDOUT_CHANNEL

from haku.sandbox_mcp.config import EnvironmentConfig
from haku.sandbox_mcp.models import (
    BootstrapState,
    DisposeSandboxResult,
    SandboxExecResult,
    SandboxInfo,
    SandboxListPage,
    SandboxState,
)
from mcp_infra.exec.models import ExecStream, Exited, Killed, TimedOut, TruncatedStream

logger = logging.getLogger(__name__)

CLAIM_GROUP = "extensions.agents.x-k8s.io"
SANDBOX_GROUP = "agents.x-k8s.io"
API_VERSION = "v1beta1"
CLAIMS_PLURAL = "sandboxclaims"
SANDBOXES_PLURAL = "sandboxes"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "haku-sandbox-mcp"
CONFIG_HASH_ANNOTATION = "haku.allegedly.works/sandbox-config-hash"
BOOTSTRAP_STATE_ANNOTATION = "haku.allegedly.works/sandbox-bootstrap-state"
BOOTSTRAP_STARTED_AT_ANNOTATION = "haku.allegedly.works/sandbox-bootstrap-started-at"
BOOTSTRAP_COMPLETED_AT_ANNOTATION = "haku.allegedly.works/sandbox-bootstrap-completed-at"

_POLL_INTERVAL_SECONDS = 1.0
_RENEW_ATTEMPTS = 4
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


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit: Exited | TimedOut | Killed
    stdout: ExecStream
    stderr: ExecStream
    duration_seconds: float


class ExecRunner(Protocol):
    async def run(
        self,
        *,
        pod_name: str,
        namespace: str,
        container: str,
        script: str,
        cwd: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> CommandResult: ...


class _ExecWebSocket(Protocol):
    async def receive(self) -> WSMessage: ...

    def exception(self) -> BaseException | None: ...


class _ExecWebSocketContext(Protocol):
    async def __aenter__(self) -> _ExecWebSocket: ...

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> bool | None: ...


class _PodExecClient(Protocol):
    async def connect_get_namespaced_pod_exec(
        self,
        name: str,
        namespace: str,
        *,
        command: list[str],
        container: str,
        stderr: bool,
        stdin: bool,
        stdout: bool,
        tty: bool,
        _preload_content: bool,
    ) -> _ExecWebSocketContext: ...


class CustomObjectsClient(Protocol):
    """Typed subset of the generated dynamic custom-object API used here."""

    async def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        *,
        label_selector: str = ...,
        limit: int = ...,
        _continue: str = ...,
    ) -> dict[str, Any]: ...

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]: ...

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object: ...

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, *, body: k8s_client.V1DeleteOptions
    ) -> object: ...


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
        text = bytes(self.stored).decode("utf-8", errors="replace")
        if self.total_bytes > len(self.stored):
            return TruncatedStream(truncated_text=text, total_bytes=self.total_bytes)
        return text


class KubernetesWebSocketExecRunner:
    """Run one bounded, non-interactive Bash script through ``pods/exec``."""

    def __init__(self, configuration: Configuration) -> None:
        self._configuration = configuration

    async def run(
        self,
        *,
        pod_name: str,
        namespace: str,
        container: str,
        script: str,
        cwd: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> CommandResult:
        stdout = _Capture(max_output_bytes)
        stderr = _Capture(max_output_bytes)
        error_data = bytearray()
        shell_script = f"cd -- {shlex.quote(cwd)}\n{script}"
        command = [
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{timeout_seconds}s",
            "bash",
            "-lc",
            shell_script,
        ]
        loop = asyncio.get_running_loop()
        started = loop.time()

        try:
            async with asyncio.timeout(timeout_seconds + 15):
                async with WsApiClient(configuration=self._configuration) as ws_api:
                    core_v1 = cast(_PodExecClient, k8s_client.CoreV1Api(api_client=ws_api))
                    websocket = await core_v1.connect_get_namespaced_pod_exec(
                        pod_name,
                        namespace,
                        command=command,
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
                                remaining = 64 * 1024 - len(error_data)
                                if remaining > 0:
                                    error_data.extend(chunk[:remaining])
        except TimeoutError:
            return CommandResult(
                exit=TimedOut(), stdout=stdout.render(), stderr=stderr.render(), duration_seconds=loop.time() - started
            )
        except ApiException as error:
            raise ToolError(_api_error("execute the sandbox command", error)) from error

        if not error_data:
            raise ToolError("Kubernetes exec ended without a command status frame; retry or inspect the sandbox")
        try:
            exit_code = WsApiClient.parse_error_data(bytes(error_data))
        except (KeyError, TypeError, ValueError) as error:
            raise ToolError("Kubernetes exec returned a malformed command status frame") from error

        exit_status: Exited | TimedOut | Killed
        if exit_code == 124:
            exit_status = TimedOut()
        elif exit_code >= 128:
            exit_status = Killed(signal=exit_code - 128)
        else:
            exit_status = Exited(exit_code=exit_code)
        return CommandResult(
            exit=exit_status, stdout=stdout.render(), stderr=stderr.render(), duration_seconds=loop.time() - started
        )


class KubernetesSandboxClient:
    """Claim lifecycle, bootstrap, status, and execution orchestration."""

    def __init__(
        self,
        environment: EnvironmentConfig,
        *,
        api_client: ApiClient,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        exec_runner: ExecRunner,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._environment = environment
        self._api_client = api_client
        self._custom_objects = custom_objects
        self._core_v1 = core_v1
        self._exec_runner = exec_runner
        self._now = now or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        await self._api_client.close()

    async def ready(self) -> None:
        try:
            await self._custom_objects.list_namespaced_custom_object(
                CLAIM_GROUP, API_VERSION, self._environment.sandbox.namespace, CLAIMS_PLURAL, limit=1
            )
        except ApiException as error:
            raise ToolError(_api_error("probe SandboxClaim access", error)) from error

    async def provision(self, name: str) -> SandboxInfo:
        claim = await self._create_or_adopt_claim(name)
        self._require_current_contract(claim)
        deadline = asyncio.get_running_loop().time() + self._environment.sandbox.provisioning_timeout_seconds
        while True:
            info = await self.info(name)
            if info.state == "stale_config":
                raise ToolError(
                    f"sandbox {name!r} was created with different server configuration; dispose and recreate it"
                )
            if info.state == "unhealthy" and info.reason in _FATAL_ALLOCATION_REASONS:
                raise ToolError(f"sandbox {name!r} provisioning failed: {info.reason}: {info.message or ''}".rstrip())
            if info.state == "ready" and info.bootstrap_state in {"succeeded", "failed"}:
                return info
            if info.state == "ready" and info.bootstrap_state == "pending":
                return await self._run_bootstrap(name, info)
            if asyncio.get_running_loop().time() >= deadline:
                return info
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def execute(
        self, *, name: str, script: str, cwd: str | None, timeout_seconds: int, max_output_bytes: int
    ) -> SandboxExecResult:
        sandbox = self._environment.sandbox
        if timeout_seconds > sandbox.max_exec_timeout_seconds:
            raise ToolError(
                f"timeout_seconds {timeout_seconds} exceeds the configured maximum {sandbox.max_exec_timeout_seconds}"
            )
        if max_output_bytes > sandbox.max_output_bytes:
            raise ToolError(
                f"max_output_bytes {max_output_bytes} exceeds the configured maximum {sandbox.max_output_bytes}"
            )
        expires_at = await self._renew(name)
        info = await self.info(name)
        if info.state != "ready" or info.bootstrap_state not in {"succeeded", "failed"} or info.pod_name is None:
            raise ToolError(
                f"sandbox {name!r} cannot execute commands (state={info.state}, "
                f"bootstrap_state={info.bootstrap_state}, reason={info.reason or 'unknown'}); "
                "call get_sandbox_info"
            )
        result = await self._exec_runner.run(
            pod_name=info.pod_name,
            namespace=sandbox.namespace,
            container=sandbox.container,
            script=script,
            cwd=cwd or sandbox.default_cwd,
            max_output_bytes=max_output_bytes,
            timeout_seconds=timeout_seconds,
        )
        return SandboxExecResult(
            exit=result.exit,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            expires_at=expires_at,
        )

    async def info(self, name: str) -> SandboxInfo:
        claim = await self._get_claim(name)
        if claim is None:
            raise ToolError(f"sandbox {name!r} was not found; call list_sandboxes or provision_sandbox")
        self._require_owned(claim, name)

        expires_at = _claim_expiry(claim)
        created_at = _metadata_timestamp(claim, "creationTimestamp")
        bootstrap_state = self._bootstrap_state(claim)
        if expires_at <= self._now():
            return _info(
                name,
                "expired",
                expires_at,
                bootstrap_state,
                created_at=created_at,
                reason="ClaimExpired",
                message="The sandbox deadline has passed.",
            )
        if not self._matches_current_contract(claim):
            return _info(
                name,
                "stale_config",
                expires_at,
                bootstrap_state,
                created_at=created_at,
                reason="ConfigurationChanged",
                message="Dispose and recreate this sandbox before provisioning or executing.",
            )

        claim_ready = _condition(claim, "Ready")
        reason = _condition_text(claim_ready, "reason")
        message = _condition_text(claim_ready, "message")
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            state: SandboxState = "unhealthy" if reason in _FATAL_ALLOCATION_REASONS else "provisioning"
            return _info(
                name, state, expires_at, bootstrap_state, created_at=created_at, reason=reason, message=message
            )

        sandbox = await self._get_custom(SANDBOX_GROUP, SANDBOXES_PLURAL, sandbox_name, "inspect Sandbox")
        if sandbox is None:
            return _info(
                name,
                "unhealthy",
                expires_at,
                bootstrap_state,
                created_at=created_at,
                sandbox_name=sandbox_name,
                reason="SandboxMissing",
                message="The SandboxClaim references a Sandbox that does not exist.",
            )
        annotations = sandbox.get("metadata", {}).get("annotations", {}) or {}
        pod_name = str(annotations.get(POD_NAME_ANNOTATION) or sandbox_name)
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, self._environment.sandbox.namespace)
        except ApiException as error:
            if error.status == 404:
                return _info(
                    name,
                    "provisioning",
                    expires_at,
                    bootstrap_state,
                    created_at=created_at,
                    sandbox_name=sandbox_name,
                    pod_name=pod_name,
                    reason="PodMissing",
                    message="The Sandbox pod has not been created yet.",
                )
            raise ToolError(_api_error("inspect the Sandbox pod", error)) from error

        claim_is_ready = claim_ready is not None and claim_ready.get("status") == "True"
        sandbox_ready = _condition(sandbox, "Ready")
        sandbox_is_ready = sandbox_ready is not None and sandbox_ready.get("status") == "True"
        pod_is_ready = _pod_is_ready(pod, self._environment.sandbox.container)
        resources_ready = claim_is_ready and sandbox_is_ready and pod_is_ready
        if not resources_ready:
            return _info(
                name,
                "provisioning",
                expires_at,
                bootstrap_state,
                created_at=created_at,
                sandbox_name=sandbox_name,
                pod_name=pod_name,
                reason=reason or _condition_text(sandbox_ready, "reason") or "PodNotReady",
                message=message or "The claim, Sandbox, or target container is not ready.",
            )
        return _info(
            name,
            "ready",
            expires_at,
            bootstrap_state,
            created_at=created_at,
            healthy=True,
            sandbox_name=sandbox_name,
            pod_name=pod_name,
            reason=reason,
            message=message,
        )

    async def list(self, *, limit: int, continue_token: str | None) -> SandboxListPage:
        if not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        try:
            list_kwargs: dict[str, Any] = {"label_selector": f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}", "limit": limit}
            if continue_token is not None:
                list_kwargs["_continue"] = continue_token
            page = await self._custom_objects.list_namespaced_custom_object(
                CLAIM_GROUP, API_VERSION, self._environment.sandbox.namespace, CLAIMS_PLURAL, **list_kwargs
            )
        except ApiException as error:
            raise ToolError(_api_error("list sandbox claims", error)) from error
        infos: list[SandboxInfo] = []
        for claim in page.get("items", []):
            name = _nested_string(claim, "metadata", "name")
            if name is not None:
                infos.append(await self.info(name))
        next_token = _nested_string(page, "metadata", "continue")
        return SandboxListPage(sandboxes=infos, continue_token=next_token)

    async def dispose(self, name: str) -> DisposeSandboxResult:
        claim = await self._get_claim(name)
        if claim is None:
            return DisposeSandboxResult(name=name, deleted=False)
        self._require_owned(claim, name)
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                CLAIM_GROUP,
                API_VERSION,
                self._environment.sandbox.namespace,
                CLAIMS_PLURAL,
                name,
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as error:
            if error.status == 404:
                return DisposeSandboxResult(name=name, deleted=False)
            raise ToolError(_api_error("dispose the sandbox claim", error)) from error
        return DisposeSandboxResult(name=name, deleted=True)

    async def _create_or_adopt_claim(self, name: str) -> dict[str, Any]:
        expires_at = self._now() + timedelta(seconds=self._environment.sandbox.initial_ttl_seconds)
        body = {
            "apiVersion": f"{CLAIM_GROUP}/{API_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {
                "name": name,
                "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                "annotations": {
                    CONFIG_HASH_ANNOTATION: self._environment.contract_hash,
                    BOOTSTRAP_STATE_ANNOTATION: "pending",
                },
            },
            "spec": {
                "warmPoolRef": {"name": self._environment.sandbox.warm_pool},
                "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": _format_timestamp(expires_at)},
            },
        }
        try:
            return await self._custom_objects.create_namespaced_custom_object(
                CLAIM_GROUP, API_VERSION, self._environment.sandbox.namespace, CLAIMS_PLURAL, body
            )
        except ApiException as error:
            if error.status != 409:
                raise ToolError(_api_error("create the SandboxClaim", error)) from error
        claim = await self._get_claim(name)
        if claim is None:
            raise ToolError(f"sandbox {name!r} already existed but disappeared during adoption; retry")
        self._require_owned(claim, name)
        return claim

    async def _run_bootstrap(self, name: str, info: SandboxInfo) -> SandboxInfo:
        if info.pod_name is None:
            raise ToolError(f"sandbox {name!r} has no ready pod for bootstrap")
        await self._patch_annotations(
            name,
            {BOOTSTRAP_STATE_ANNOTATION: "running", BOOTSTRAP_STARTED_AT_ANNOTATION: _format_timestamp(self._now())},
        )
        bootstrap = self._environment.bootstrap
        result = await self._exec_runner.run(
            pod_name=info.pod_name,
            namespace=self._environment.sandbox.namespace,
            container=self._environment.sandbox.container,
            script=bootstrap.script,
            cwd=bootstrap.cwd,
            max_output_bytes=self._environment.sandbox.max_output_bytes,
            timeout_seconds=bootstrap.timeout_seconds,
        )
        succeeded = isinstance(result.exit, Exited) and result.exit.exit_code == 0
        await self._patch_annotations(
            name,
            {
                BOOTSTRAP_STATE_ANNOTATION: "succeeded" if succeeded else "failed",
                BOOTSTRAP_COMPLETED_AT_ANNOTATION: _format_timestamp(self._now()),
            },
        )
        if not succeeded:
            raise ToolError(
                f"sandbox {name!r} bootstrap failed ({_exit_summary(result)}); the claim was retained. "
                "Call get_sandbox_info, exec_sandbox for diagnosis, or dispose_sandbox and provision a fresh claim."
            )
        return await self.info(name)

    async def _renew(self, name: str) -> datetime:
        for attempt in range(_RENEW_ATTEMPTS):
            claim = await self._get_claim(name)
            if claim is None:
                raise ToolError(f"sandbox {name!r} was not found; provision it first")
            self._require_owned(claim, name)
            self._require_current_contract(claim)
            current = _claim_expiry(claim)
            now = self._now()
            if current <= now:
                raise ToolError(f"sandbox {name!r} has expired; dispose and provision it again")
            target = max(current, now + timedelta(seconds=self._environment.sandbox.exec_ttl_extension_seconds))
            if target == current:
                return current
            resource_version = _nested_string(claim, "metadata", "resourceVersion")
            if resource_version is None:
                raise ToolError(f"sandbox {name!r} claim has no Kubernetes resourceVersion")
            patch = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
                {"op": "replace", "path": "/spec/lifecycle/shutdownTime", "value": _format_timestamp(target)},
            ]
            try:
                await self._custom_objects.patch_namespaced_custom_object(
                    CLAIM_GROUP,
                    API_VERSION,
                    self._environment.sandbox.namespace,
                    CLAIMS_PLURAL,
                    name,
                    patch,
                    _content_type="application/json-patch+json",
                )
                return target
            except ApiException as error:
                if error.status == 409 and attempt + 1 < _RENEW_ATTEMPTS:
                    continue
                raise ToolError(
                    f"{_api_error('refresh the sandbox deadline', error)}; command was not executed"
                ) from error
        raise AssertionError("unreachable")

    async def _patch_annotations(self, name: str, annotations: dict[str, str]) -> None:
        try:
            await self._custom_objects.patch_namespaced_custom_object(
                CLAIM_GROUP,
                API_VERSION,
                self._environment.sandbox.namespace,
                CLAIMS_PLURAL,
                name,
                {"metadata": {"annotations": annotations}},
                _content_type="application/merge-patch+json",
            )
        except ApiException as error:
            raise ToolError(_api_error("record sandbox bootstrap state", error)) from error

    async def _get_claim(self, name: str) -> dict[str, Any] | None:
        return await self._get_custom(CLAIM_GROUP, CLAIMS_PLURAL, name, "inspect SandboxClaim")

    async def _get_custom(self, group: str, plural: str, name: str, action: str) -> dict[str, Any] | None:
        try:
            return await self._custom_objects.get_namespaced_custom_object(
                group, API_VERSION, self._environment.sandbox.namespace, plural, name
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise ToolError(_api_error(action, error)) from error

    def _require_owned(self, claim: dict[str, Any], name: str) -> None:
        labels = claim.get("metadata", {}).get("labels", {}) or {}
        if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
            raise ToolError(f"SandboxClaim {name!r} exists but is not owned by this MCP server")

    def _matches_current_contract(self, claim: dict[str, Any]) -> bool:
        annotations = claim.get("metadata", {}).get("annotations", {}) or {}
        return annotations.get(CONFIG_HASH_ANNOTATION) == self._environment.contract_hash

    def _require_current_contract(self, claim: dict[str, Any]) -> None:
        if not self._matches_current_contract(claim):
            name = _nested_string(claim, "metadata", "name") or "<unknown>"
            raise ToolError(
                f"sandbox {name!r} was created with different server configuration; dispose and recreate it"
            )

    def _bootstrap_state(self, claim: dict[str, Any]) -> BootstrapState:
        """Treat an interrupted bootstrap as failed after its execution budget."""

        annotations = claim.get("metadata", {}).get("annotations", {}) or {}
        raw = str(annotations.get(BOOTSTRAP_STATE_ANNOTATION, "pending"))
        if not _is_bootstrap_state(raw):
            raise ToolError(f"sandbox claim has invalid bootstrap state {raw!r}")
        if raw != "running":
            return raw
        started_raw = annotations.get(BOOTSTRAP_STARTED_AT_ANNOTATION)
        if not started_raw:
            return "failed"
        started_at = _parse_timestamp(str(started_raw), f"annotation {BOOTSTRAP_STARTED_AT_ANNOTATION}")
        failure_at = started_at + timedelta(seconds=self._environment.bootstrap.timeout_seconds + 15)
        return "failed" if self._now() >= failure_at else "running"


class InClusterSandboxClient:
    """Lazily initialize the in-cluster Kubernetes clients."""

    def __init__(self, environment: EnvironmentConfig) -> None:
        self._environment = environment
        self._client: KubernetesSandboxClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> KubernetesSandboxClient:
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
                self._client = KubernetesSandboxClient(
                    self._environment,
                    api_client=api_client,
                    custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api_client)),
                    core_v1=CoreV1Api(api_client),
                    exec_runner=KubernetesWebSocketExecRunner(configuration),
                )
        return self._client

    async def provision(self, name: str) -> SandboxInfo:
        return await (await self._get_client()).provision(name)

    async def execute(
        self, *, name: str, script: str, cwd: str | None, timeout_seconds: int, max_output_bytes: int
    ) -> SandboxExecResult:
        return await (await self._get_client()).execute(
            name=name, script=script, cwd=cwd, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes
        )

    async def info(self, name: str) -> SandboxInfo:
        return await (await self._get_client()).info(name)

    async def list(self, *, limit: int, continue_token: str | None) -> SandboxListPage:
        return await (await self._get_client()).list(limit=limit, continue_token=continue_token)

    async def dispose(self, name: str) -> DisposeSandboxResult:
        return await (await self._get_client()).dispose(name)

    async def ready(self) -> None:
        await (await self._get_client()).ready()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    for condition in resource.get("status", {}).get("conditions", []) or []:
        if isinstance(condition, dict) and condition.get("type") == condition_type:
            return {str(key): value for key, value in condition.items()}
    return None


def _is_bootstrap_state(value: str) -> TypeGuard[BootstrapState]:
    return value in {"pending", "running", "succeeded", "failed"}


def _condition_text(condition: dict[str, Any] | None, key: str) -> str | None:
    if condition is None or not condition.get(key):
        return None
    return str(condition[key])


def _nested_string(value: dict[str, Any], *path: str) -> str | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return str(current) if current not in {None, ""} else None


def _claim_expiry(claim: dict[str, Any]) -> datetime:
    raw = _nested_string(claim, "spec", "lifecycle", "shutdownTime")
    if raw is None:
        raise ToolError("sandbox claim has no lifecycle shutdownTime")
    return _parse_timestamp(raw, "sandbox claim shutdownTime")


def _metadata_timestamp(resource: dict[str, Any], key: str) -> datetime | None:
    raw = _nested_string(resource, "metadata", key)
    return _parse_timestamp(raw, f"metadata.{key}") if raw is not None else None


def _parse_timestamp(raw: str, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ToolError(f"{field} is not a valid RFC 3339 timestamp") from error
    if value.tzinfo is None:
        raise ToolError(f"{field} has no timezone")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _pod_is_ready(pod: Any, container: str) -> bool:
    status = getattr(pod, "status", None)
    if status is None or status.phase != "Running":
        return False
    return any(item.name == container and item.ready for item in (status.container_statuses or []))


def _info(
    name: str,
    state: SandboxState,
    expires_at: datetime,
    bootstrap_state: BootstrapState,
    *,
    created_at: datetime | None = None,
    healthy: bool = False,
    sandbox_name: str | None = None,
    pod_name: str | None = None,
    reason: str | None = None,
    message: str | None = None,
) -> SandboxInfo:
    return SandboxInfo(
        name=name,
        state=state,
        healthy=healthy,
        created_at=created_at,
        expires_at=expires_at,
        sandbox_name=sandbox_name,
        pod_name=pod_name,
        bootstrap_state=bootstrap_state,
        reason=reason,
        message=message,
    )


def _exit_summary(result: CommandResult) -> str:
    match result.exit:
        case Exited(exit_code=code):
            return f"exit code {code}"
        case TimedOut():
            return "timed out"
        case Killed(signal=signal):
            return f"killed by signal {signal}"


def _api_error(action: str, error: ApiException) -> str:
    reason = f": {error.reason}" if error.reason else ""
    return f"Kubernetes could not {action} (HTTP {error.status or 'unknown'}{reason})"
