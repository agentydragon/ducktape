"""Shared construction and creation of Agent Sandbox claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient

CLAIM_GROUP = "extensions.agents.x-k8s.io"
CLAIM_API_VERSION = "v1beta1"
CLAIMS_PLURAL = "sandboxclaims"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "haku-console"


class _ClaimMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    labels: dict[str, str]
    annotations: dict[str, str] | None = None


class _ClaimWarmPoolRef(BaseModel):
    name: str


class _ClaimLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    shutdown_policy: str = Field(alias="shutdownPolicy")
    shutdown_time: str = Field(alias="shutdownTime")


class _ClaimEnvVar(BaseModel):
    name: str
    value: str


class _ClaimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    warm_pool_ref: _ClaimWarmPoolRef = Field(alias="warmPoolRef")
    lifecycle: _ClaimLifecycle
    env: list[_ClaimEnvVar] | None = None


class _SandboxClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    kind: str
    metadata: _ClaimMetadata
    spec: _ClaimSpec


@dataclass(frozen=True, slots=True)
class SandboxAllocationSpec:
    """Inputs for one claim, including env injected only when the claim is created.

    The env payload is creation-time-only: an adoption path leaves the existing claim, including
    its env, untouched. Callers retain ownership of their orchestration and supply their own
    labels, annotations, and lifecycle policy here.
    """

    namespace: str
    name: str
    warm_pool: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    shutdown_policy: str
    shutdown_time: datetime
    env: Mapping[str, str] | None = None


def format_shutdown_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_sandbox_claim(spec: SandboxAllocationSpec) -> dict[str, Any]:
    return _SandboxClaim(
        apiVersion=f"{CLAIM_GROUP}/{CLAIM_API_VERSION}",
        kind="SandboxClaim",
        metadata={
            "name": spec.name,
            "labels": dict(spec.labels),
            "annotations": dict(spec.annotations) or None,
        },
        spec={
            "warmPoolRef": {"name": spec.warm_pool},
            "lifecycle": {
                "shutdownPolicy": spec.shutdown_policy,
                "shutdownTime": format_shutdown_time(spec.shutdown_time),
            },
            "env": (
                [{"name": name, "value": value} for name, value in spec.env.items()]
                if spec.env is not None
                else None
            ),
        },
    ).model_dump(by_alias=True, exclude_none=True)


async def create_sandbox_claim(custom_objects: CustomObjectsClient, spec: SandboxAllocationSpec) -> dict[str, Any]:
    """Build and create a claim; adoption remains the caller's orchestration concern."""

    return await custom_objects.create_namespaced_custom_object(
        CLAIM_GROUP, CLAIM_API_VERSION, spec.namespace, CLAIMS_PLURAL, build_sandbox_claim(spec)
    )

SANDBOX_GROUP = "agents.x-k8s.io"
SANDBOX_API_VERSION = "v1beta1"
SANDBOXES_PLURAL = "sandboxes"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


@dataclass(frozen=True, slots=True)
class SandboxClaimGraph:
    """One best-effort snapshot of a claim and the resources it names.

    A missing claim, Sandbox, or Pod is represented by a null resource rather than hidden behind
    caller-specific polling policy. The raw resources remain available so each caller can project
    the same graph into its own status model.
    """

    claim_name: str
    claim: dict[str, Any] | None
    sandbox_name: str | None
    sandbox: dict[str, Any] | None
    pod_name: str | None
    pod: Any | None


class SandboxClaimClient:
    """Shared raw SandboxClaim lifecycle and Claim -> Sandbox -> Pod graph access."""

    def __init__(
        self,
        custom_objects: CustomObjectsClient,
        core_v1: Any,
        namespace: str,
        *,
    ) -> None:
        self._custom_objects = custom_objects
        self._core_v1 = core_v1
        self._namespace = namespace

    async def create(self, spec: SandboxAllocationSpec) -> dict[str, Any]:
        """Create a claim from the shared allocation model.

        `spec.env` is sent only here. Adoption never mutates the existing claim, so its env stays
        whatever was committed at creation time.
        """

        return await create_sandbox_claim(self._custom_objects, spec)

    async def get(self, name: str) -> dict[str, Any] | None:
        try:
            return await self._custom_objects.get_namespaced_custom_object(
                CLAIM_GROUP, CLAIM_API_VERSION, self._namespace, CLAIMS_PLURAL, name
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    async def delete(self, name: str) -> bool:
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                CLAIM_GROUP,
                CLAIM_API_VERSION,
                self._namespace,
                CLAIMS_PLURAL,
                name,
                body={"propagationPolicy": "Foreground"},
            )
        except ApiException as error:
            if error.status == 404:
                return False
            raise
        return True

    async def list(
        self, *, limit: int, continue_token: str | None = None, label_selector: str | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"limit": limit}
        if continue_token is not None:
            kwargs["_continue"] = continue_token
        if label_selector is not None:
            kwargs["label_selector"] = label_selector
        return await self._custom_objects.list_namespaced_custom_object(
            CLAIM_GROUP, CLAIM_API_VERSION, self._namespace, CLAIMS_PLURAL, **kwargs
        )

    async def patch_annotations(self, name: str, annotations: Mapping[str, str]) -> None:
        await self._custom_objects.patch_namespaced_custom_object(
            CLAIM_GROUP,
            CLAIM_API_VERSION,
            self._namespace,
            CLAIMS_PLURAL,
            name,
            {"metadata": {"annotations": dict(annotations)}},
            _content_type="application/merge-patch+json",
        )

    async def renew(self, name: str, shutdown_time: datetime, *, attempts: int = 4) -> bool:
        """Renew a claim with resource-version protection; return false when it is absent."""

        for attempt in range(attempts):
            claim = await self.get(name)
            if claim is None:
                return False
            resource_version = _nested_string(claim, "metadata", "resourceVersion")
            if resource_version is None:
                raise ValueError(f"SandboxClaim {name!r} has no Kubernetes resourceVersion")
            patch = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
                {
                    "op": "replace",
                    "path": "/spec/lifecycle/shutdownTime",
                    "value": format_shutdown_time(shutdown_time),
                },
            ]
            try:
                await self._custom_objects.patch_namespaced_custom_object(
                    CLAIM_GROUP,
                    CLAIM_API_VERSION,
                    self._namespace,
                    CLAIMS_PLURAL,
                    name,
                    patch,
                    _content_type="application/json-patch+json",
                )
                return True
            except ApiException as error:
                if error.status == 409 and attempt + 1 < attempts:
                    continue
                raise
        raise AssertionError("unreachable")

    async def graph(self, name: str) -> SandboxClaimGraph:
        """Read the claim graph; missing descendants remain explicit in the snapshot."""

        claim = await self.get(name)
        if claim is None:
            return SandboxClaimGraph(name, None, None, None, None, None)
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            return SandboxClaimGraph(name, claim, None, None, None, None)
        try:
            sandbox = await self._custom_objects.get_namespaced_custom_object(
                SANDBOX_GROUP, SANDBOX_API_VERSION, self._namespace, SANDBOXES_PLURAL, sandbox_name
            )
        except ApiException as error:
            if error.status == 404:
                return SandboxClaimGraph(name, claim, sandbox_name, None, None, None)
            raise
        annotations = sandbox.get("metadata", {}).get("annotations", {}) or {}
        pod_name = str(annotations.get(POD_NAME_ANNOTATION) or sandbox_name)
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, self._namespace)
        except ApiException as error:
            if error.status == 404:
                return SandboxClaimGraph(name, claim, sandbox_name, sandbox, pod_name, None)
            raise
        return SandboxClaimGraph(name, claim, sandbox_name, sandbox, pod_name, pod)


def _nested_string(value: dict[str, Any], *path: str) -> str | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return str(current) if current not in {None, ""} else None
