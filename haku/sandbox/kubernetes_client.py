"""Kubernetes implementation over the external Agent Sandbox CRDs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard, cast

from fastmcp.exceptions import ToolError
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, ApiException, Configuration, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException

from haku.sandbox.config import SandboxEnvironmentConfig
from haku.sandbox.models import (
    BootstrapState,
    DisposeSandboxResult,
    SandboxExecResult,
    SandboxInfo,
    SandboxListPage,
    SandboxState,
    SandboxWarning,
    SandboxWarningKind,
)
from mcp_infra.exec.kubernetes import CommandResult, ExecRunner, KubernetesWebSocketExecRunner, PodExecError
from mcp_infra.exec.models import Exited, Killed, TimedOut
from util.agent_sandbox import (
    CLAIMS_PLURAL,
    EXTENSIONS_API,
    POD_NAME_ANNOTATION,
    SANDBOX_API,
    SANDBOXES_PLURAL,
    Api,
    condition,
)
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)


MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
# Ownership marker written on every claim and required by every mutation, so it is a stored
# value rather than a name: changing it orphans live claims, which then match nothing and can
# be neither adopted nor disposed. It still reads "-mcp" from when this ran as its own service.
MANAGED_BY_VALUE = "haku-sandbox-mcp"
WARM_POOL_ANNOTATION = "haku.allegedly.works/sandbox-warm-pool"
CONTAINER_ANNOTATION = "haku.allegedly.works/sandbox-container"
DEFAULT_CWD_ANNOTATION = "haku.allegedly.works/sandbox-default-cwd"
BOOTSTRAP_HASH_ANNOTATION = "haku.allegedly.works/sandbox-bootstrap-hash"
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
class _ProvenanceField:
    """One annotated property of the box a claim was created for, and its configured counterpart.

    Only properties that genuinely describe the running pod are recorded. Per-call and lifecycle
    budgets (output and exec ceilings, TTLs, provisioning timeout) are read live from the current
    configuration and are never grounds to consider a claim divergent.
    """

    kind: SandboxWarningKind
    annotation: str
    description: str
    configured: str


class KubernetesSandboxClient:
    """Claim lifecycle, bootstrap, status, and execution orchestration."""

    def __init__(
        self,
        environment: SandboxEnvironmentConfig,
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

    async def _exec(self, **kwargs: Any) -> CommandResult:
        """Run through the shared runner, which knows nothing of MCP, and owe this surface a ToolError."""
        try:
            return await self._exec_runner.run(**kwargs)
        except PodExecError as error:
            raise ToolError(str(error)) from error

    async def aclose(self) -> None:
        await self._api_client.close()

    async def provision(self, name: str) -> SandboxInfo:
        await self._create_or_adopt_claim(name)
        deadline = asyncio.get_running_loop().time() + self._environment.sandbox.provisioning_timeout_seconds
        while True:
            info = await self.info(name)
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
        result = await self._exec(
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
        warnings = self._divergences(claim, bootstrap_state)
        if expires_at <= self._now():
            return _info(
                name,
                "expired",
                expires_at,
                bootstrap_state,
                warnings,
                created_at=created_at,
                reason="ClaimExpired",
                message="The sandbox deadline has passed.",
            )

        claim_ready = condition(claim, "Ready")
        reason = _condition_text(claim_ready, "reason")
        message = _condition_text(claim_ready, "message")
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            state: SandboxState = "unhealthy" if reason in _FATAL_ALLOCATION_REASONS else "provisioning"
            return _info(
                name,
                state,
                expires_at,
                bootstrap_state,
                warnings,
                created_at=created_at,
                reason=reason,
                message=message,
            )

        sandbox = await self._get_custom(SANDBOX_API, SANDBOXES_PLURAL, sandbox_name, "inspect Sandbox")
        if sandbox is None:
            return _info(
                name,
                "unhealthy",
                expires_at,
                bootstrap_state,
                warnings,
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
                    warnings,
                    created_at=created_at,
                    sandbox_name=sandbox_name,
                    pod_name=pod_name,
                    reason="PodMissing",
                    message="The Sandbox pod has not been created yet.",
                )
            raise ToolError(_api_error("inspect the Sandbox pod", error)) from error

        claim_is_ready = claim_ready is not None and claim_ready.get("status") == "True"
        sandbox_ready = condition(sandbox, "Ready")
        sandbox_is_ready = sandbox_ready is not None and sandbox_ready.get("status") == "True"
        pod_is_ready = _pod_is_ready(pod, self._environment.sandbox.container)
        resources_ready = claim_is_ready and sandbox_is_ready and pod_is_ready
        if not resources_ready:
            return _info(
                name,
                "provisioning",
                expires_at,
                bootstrap_state,
                warnings,
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
            warnings,
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
                *EXTENSIONS_API, self._environment.sandbox.namespace, CLAIMS_PLURAL, **list_kwargs
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
                *EXTENSIONS_API,
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
            "apiVersion": EXTENSIONS_API.api_version,
            "kind": "SandboxClaim",
            "metadata": {
                "name": name,
                "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                "annotations": {
                    WARM_POOL_ANNOTATION: self._environment.sandbox.warm_pool,
                    CONTAINER_ANNOTATION: self._environment.sandbox.container,
                    DEFAULT_CWD_ANNOTATION: self._environment.sandbox.default_cwd,
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
                *EXTENSIONS_API, self._environment.sandbox.namespace, CLAIMS_PLURAL, body
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
        bootstrap = self._environment.bootstrap
        # Stamped from the script about to run, not at claim creation: until the bootstrap starts,
        # the claim will be set up by whatever config is current, so there is nothing to diverge from.
        await self._patch_annotations(
            name,
            {
                BOOTSTRAP_STATE_ANNOTATION: "running",
                BOOTSTRAP_STARTED_AT_ANNOTATION: _format_timestamp(self._now()),
                BOOTSTRAP_HASH_ANNOTATION: bootstrap.script_digest,
            },
        )
        result = await self._exec(
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
                    *EXTENSIONS_API,
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
                *EXTENSIONS_API,
                self._environment.sandbox.namespace,
                CLAIMS_PLURAL,
                name,
                {"metadata": {"annotations": annotations}},
                _content_type="application/merge-patch+json",
            )
        except ApiException as error:
            raise ToolError(_api_error("record sandbox bootstrap state", error)) from error

    async def _get_claim(self, name: str) -> dict[str, Any] | None:
        return await self._get_custom(EXTENSIONS_API, CLAIMS_PLURAL, name, "inspect SandboxClaim")

    async def _get_custom(self, api: Api, plural: str, name: str, action: str) -> dict[str, Any] | None:
        try:
            return await self._custom_objects.get_namespaced_custom_object(
                *api, self._environment.sandbox.namespace, plural, name
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise ToolError(_api_error(action, error)) from error

    def _require_owned(self, claim: dict[str, Any], name: str) -> None:
        labels = claim.get("metadata", {}).get("labels", {}) or {}
        if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
            raise ToolError(f"SandboxClaim {name!r} exists but is not owned by this MCP server")

    # Annotated as tuples because `list` resolves to this class's `list` method in a signature.
    def _provenance_fields(self, bootstrap_state: BootstrapState) -> tuple[_ProvenanceField, ...]:
        sandbox = self._environment.sandbox
        fields = [
            _ProvenanceField("warm_pool_changed", WARM_POOL_ANNOTATION, "warm pool", sandbox.warm_pool),
            _ProvenanceField("container_changed", CONTAINER_ANNOTATION, "container", sandbox.container),
            _ProvenanceField(
                "default_cwd_changed", DEFAULT_CWD_ANNOTATION, "default working directory", sandbox.default_cwd
            ),
        ]
        if bootstrap_state != "pending":
            fields.append(
                _ProvenanceField(
                    "bootstrap_script_changed",
                    BOOTSTRAP_HASH_ANNOTATION,
                    "bootstrap script digest",
                    self._environment.bootstrap.script_digest,
                )
            )
        return tuple(fields)

    def _divergences(self, claim: dict[str, Any], bootstrap_state: BootstrapState) -> tuple[SandboxWarning, ...]:
        """Report how the claim's recorded environment differs from the configured one.

        Annotations this Console does not know are ignored rather than rejected, so a claim written
        by a newer replica during a rolling deploy stays readable.
        """

        annotations = claim.get("metadata", {}).get("annotations", {}) or {}
        warnings: list[SandboxWarning] = []
        unrecorded: list[str] = []
        for provenance in self._provenance_fields(bootstrap_state):
            recorded = annotations.get(provenance.annotation)
            if recorded is None:
                unrecorded.append(provenance.description)
            elif str(recorded) != provenance.configured:
                warnings.append(
                    SandboxWarning(
                        kind=provenance.kind,
                        detail=(
                            f"claim records {provenance.description} {str(recorded)!r}; "
                            f"configuration now names {provenance.configured!r}"
                        ),
                    )
                )
        if unrecorded:
            warnings.append(
                SandboxWarning(
                    kind="provenance_unknown",
                    detail=(
                        f"claim records no {', '.join(unrecorded)}, so whether it matches the current "
                        "configuration is unknown"
                    ),
                )
            )
        return tuple(warnings)

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

    def __init__(self, environment: SandboxEnvironmentConfig) -> None:
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

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _is_bootstrap_state(value: str) -> TypeGuard[BootstrapState]:
    return value in {"pending", "running", "succeeded", "failed"}


def _condition_text(entry: dict[str, Any] | None, key: str) -> str | None:
    if entry is None or not entry.get(key):
        return None
    return str(entry[key])


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
    warnings: Sequence[SandboxWarning],
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
        warnings=list(warnings),
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
