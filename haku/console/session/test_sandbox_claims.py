"""Focused contracts for the SandboxClaim one chat session runs in.

The recorded objects here are the *Kubernetes API clients*, so these tests run the real
`KubernetesSandboxClaims` — unlike `recording_claims.py`, which stands in for the claim builder
itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from haku.console.session.sandbox_claims import (
    KubernetesClients,
    KubernetesSandboxClaims,
    ProvisioningStep,
    SandboxClaimSpec,
)


class RecordingCustomObjectsApi:
    def __init__(self) -> None:
        self.created: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.patched: list[tuple[str, Any]] = []

    async def create_namespaced_custom_object(self, *args: Any, **kwargs: Any) -> None:
        self.created = (args, kwargs)

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        del group, version, namespace
        return self.objects[(plural, name)]

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: Any, **kwargs: Any
    ) -> None:
        del group, version, namespace, plural, kwargs
        self.patched.append((name, body))


class RecordingApiClient:
    """The shared client the other two are built on. Only its close is reached from a test."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingCoreV1Api:
    def __init__(self) -> None:
        self.pods: dict[str, k8s_client.V1Pod] = {}

    async def read_namespaced_pod(self, name: str, namespace: str) -> k8s_client.V1Pod:
        del namespace
        return self.pods[name]


@pytest.fixture
def custom_objects_api() -> RecordingCustomObjectsApi:
    return RecordingCustomObjectsApi()


@pytest.fixture
def core_v1_api() -> RecordingCoreV1Api:
    return RecordingCoreV1Api()


@pytest.fixture
def sandbox_claims(custom_objects_api, core_v1_api) -> KubernetesSandboxClaims:
    """The real claim builder with only the Kubernetes API objects recorded."""
    return KubernetesSandboxClaims(
        SandboxClaimSpec(
            namespace="haku-claude-sandbox",
            warm_pool="haku-claude",
            claim_prefix="claude",
            runtime_label="claude-chat",
            runner_environment={},
        ),
        KubernetesClients(
            api=cast(Any, RecordingApiClient()),
            custom_objects=cast(Any, custom_objects_api),
            core_v1=cast(Any, core_v1_api),
        ),
    )


async def test_claim_injects_the_session_token(sandbox_claims, custom_objects_api) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")

    await sandbox_claims.create(
        session_id=session_id, session_token="session-secret", expires_at=datetime(2026, 8, 1, 5, 0, tzinfo=UTC)
    )

    assert custom_objects_api.created is not None
    args, _ = custom_objects_api.created
    assert args[:4] == ("extensions.agents.x-k8s.io", "v1beta1", "haku-claude-sandbox", "sandboxclaims")
    body = args[4]
    assert body["metadata"]["name"] == "claude-10000000000040008000000000000001"
    assert body["spec"]["warmPoolRef"] == {"name": "haku-claude"}
    # Both spellings, one value: runner images from before the HAKU_SESSION_TOKEN rename read only
    # the legacy name (the CLEANUP in sandbox_claims.py names the removal condition).
    assert body["spec"]["env"] == [
        {"name": "HAKU_RUNNER_SESSION_ID", "value": str(session_id)},
        {"name": "HAKU_SESSION_TOKEN", "value": "session-secret"},
        {"name": "HAKU_RUNNER_TOKEN", "value": "session-secret"},
    ]
    assert body["spec"]["lifecycle"] == {"shutdownPolicy": "DeleteForeground", "shutdownTime": "2026-08-01T05:00:00Z"}


async def test_renew_slides_the_shutdown_time_testing_on_resource_version(sandbox_claims, custom_objects_api) -> None:
    """The deadline is a lease: renew pushes `shutdownTime` out, guarded by a `test` on the
    resourceVersion it read, so a concurrent writer's update is never clobbered."""
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    name = "claude-10000000000040008000000000000001"
    custom_objects_api.objects[("sandboxclaims", name)] = {"metadata": {"resourceVersion": "4242"}}

    await sandbox_claims.renew(session_id=session_id, expires_at=datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    [(patched_name, patch)] = custom_objects_api.patched
    assert patched_name == name
    assert patch == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "4242"},
        {"op": "replace", "path": "/spec/lifecycle/shutdownTime", "value": "2026-08-01T07:00:00Z"},
    ]


async def test_renew_is_a_no_op_when_the_claim_is_already_gone(sandbox_claims, custom_objects_api) -> None:
    """A 404 means the session is ending; renew leaves it to the lease sweep rather than raising
    and taking the heartbeat down."""
    session_id = UUID("10000000-0000-4000-8000-000000000002")
    custom_objects_api.get_namespaced_custom_object = _raise_api_error(404)

    await sandbox_claims.renew(session_id=session_id, expires_at=datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    assert custom_objects_api.patched == []


def _raise_api_error(status: int) -> Any:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise k8s_client.ApiException(status=status)

    return _raise


async def test_inspect_reports_each_underlying_provisioning_layer(
    sandbox_claims, custom_objects_api, core_v1_api
) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom_objects_api.objects[("sandboxclaims", claim_name)] = {
        "status": {
            "sandbox": {"name": "sandbox-abc"},
            "conditions": [{"type": "Ready", "status": "False", "reason": "PodNotReady", "message": "Waiting for Pod"}],
        }
    }
    custom_objects_api.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {"annotations": {"agents.x-k8s.io/pod-name": "sandbox-pod-abc"}},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    core_v1_api.pods["sandbox-pod-abc"] = k8s_client.V1Pod(
        status=k8s_client.V1PodStatus(
            phase="Pending",
            conditions=[k8s_client.V1PodCondition(type="Ready", status="False")],
            container_statuses=[
                k8s_client.V1ContainerStatus(
                    name="runner",
                    image="runner:test",
                    image_id="",
                    ready=False,
                    restart_count=0,
                    state=k8s_client.V1ContainerState(
                        waiting=k8s_client.V1ContainerStateWaiting(reason="ContainerCreating")
                    ),
                )
            ],
        )
    )

    info = await sandbox_claims.inspect(session_id=session_id)

    assert info.step == "waiting_for_pod_ready"
    assert info.claim_name == claim_name
    assert info.claim_ready is False
    assert info.claim_reason == "PodNotReady"
    assert info.claim_message == "Waiting for Pod"
    assert info.sandbox_name == "sandbox-abc"
    assert info.sandbox_ready is False
    assert info.pod_name == "sandbox-pod-abc"
    assert info.pod_phase == "Pending"
    assert info.pod_ready is False
    assert info.runner_ready is False
    assert info.runner_state == "waiting: ContainerCreating"


async def test_inspect_distinguishes_ready_pod_from_runner_bridge_wait(
    sandbox_claims, custom_objects_api, core_v1_api
) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom_objects_api.objects[("sandboxclaims", claim_name)] = {
        "status": {"sandbox": {"name": "sandbox-abc"}, "conditions": [{"type": "Ready", "status": "True"}]}
    }
    custom_objects_api.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    core_v1_api.pods["sandbox-abc"] = k8s_client.V1Pod(
        status=k8s_client.V1PodStatus(
            phase="Running",
            conditions=[k8s_client.V1PodCondition(type="Ready", status="True")],
            container_statuses=[
                k8s_client.V1ContainerStatus(
                    name="runner",
                    image="runner:test",
                    image_id="runner:test",
                    ready=True,
                    restart_count=0,
                    state=k8s_client.V1ContainerState(running=k8s_client.V1ContainerStateRunning()),
                )
            ],
        )
    )

    info = await sandbox_claims.inspect(session_id=session_id)

    assert info.step == "waiting_for_runner"
    assert info.claim_ready is True
    assert info.sandbox_ready is True
    assert info.pod_ready is True
    assert info.runner_ready is True
    assert info.runner_state == "running"


async def test_inspect_says_a_claim_is_gone_rather_than_newly_made(sandbox_claims, custom_objects_api) -> None:
    """A 404 is a claim that was never created or has been reclaimed — not the neighbouring step,
    a claim that has just been made and whose sandbox has not been assigned yet."""
    custom_objects_api.get_namespaced_custom_object = _raise_api_error(404)

    info = await sandbox_claims.inspect(session_id=UUID("10000000-0000-4000-8000-000000000002"))

    assert info.step == ProvisioningStep.CLAIM_ABSENT
    assert info.claim_name == "claude-10000000000040008000000000000002"
    assert info.observation_error is None, "the cluster answered; it just has no such claim"


if __name__ == "__main__":
    pytest_bazel.main()
