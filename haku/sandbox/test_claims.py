"""Artifact tests for both SandboxClaim compositions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiException

from haku.sandbox.claims import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SandboxAllocationSpec,
    SandboxClaimClient,
    build_sandbox_claim,
)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (
            SandboxAllocationSpec(
                namespace="haku-claude-sandbox",
                name="claude-10000000000040008000000000000001",
                warm_pool="haku-claude",
                labels={"app.kubernetes.io/managed-by": "haku-console", "haku.allegedly.works/harness": "claude"},
                annotations={},
                shutdown_policy="DeleteForeground",
                shutdown_time=datetime(2026, 8, 1, 5, 0, tzinfo=UTC),
                env={
                    "RUNNER_VAR": "environment",
                    "HAKU_RUNNER_SESSION_ID": "10000000-0000-4000-8000-000000000001",
                    "HAKU_SESSION_TOKEN": "session-secret",
                    "HAKU_RUNNER_TOKEN": "session-secret",
                },
            ),
            {
                "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
                "kind": "SandboxClaim",
                "metadata": {
                    "name": "claude-10000000000040008000000000000001",
                    "labels": {
                        "app.kubernetes.io/managed-by": "haku-console",
                        "haku.allegedly.works/harness": "claude",
                    },
                },
                "spec": {
                    "warmPoolRef": {"name": "haku-claude"},
                    "lifecycle": {"shutdownPolicy": "DeleteForeground", "shutdownTime": "2026-08-01T05:00:00Z"},
                    "env": [
                        {"name": "RUNNER_VAR", "value": "environment"},
                        {"name": "HAKU_RUNNER_SESSION_ID", "value": "10000000-0000-4000-8000-000000000001"},
                        {"name": "HAKU_SESSION_TOKEN", "value": "session-secret"},
                        {"name": "HAKU_RUNNER_TOKEN", "value": "session-secret"},
                    ],
                },
            },
        ),
        (
            SandboxAllocationSpec(
                namespace="agent-workspaces",
                name="task-one",
                warm_pool="test-pool",
                labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                annotations={
                    "haku.allegedly.works/sandbox-warm-pool": "test-pool",
                    "haku.allegedly.works/sandbox-container": "workspace",
                    "haku.allegedly.works/sandbox-default-cwd": "/test/workspace/test-state",
                    "haku.allegedly.works/sandbox-bootstrap-state": "pending",
                },
                shutdown_policy="Delete",
                shutdown_time=datetime(2026, 7, 22, 20, 0, tzinfo=UTC),
            ),
            {
                "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
                "kind": "SandboxClaim",
                "metadata": {
                    "name": "task-one",
                    "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                    "annotations": {
                        "haku.allegedly.works/sandbox-warm-pool": "test-pool",
                        "haku.allegedly.works/sandbox-container": "workspace",
                        "haku.allegedly.works/sandbox-default-cwd": "/test/workspace/test-state",
                        "haku.allegedly.works/sandbox-bootstrap-state": "pending",
                    },
                },
                "spec": {
                    "warmPoolRef": {"name": "test-pool"},
                    "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": "2026-07-22T20:00:00Z"},
                },
            },
        ),
    ],
)
def test_build_claim_matches_current_path_artifact(spec: SandboxAllocationSpec, expected: dict) -> None:
    assert build_sandbox_claim(spec) == expected


async def test_shared_claim_client_renews_with_resource_version() -> None:
    custom_objects = Mock()
    custom_objects.get_namespaced_custom_object = AsyncMock(
        return_value={"metadata": {"resourceVersion": "9"}}
    )
    custom_objects.patch_namespaced_custom_object = AsyncMock()
    client = SandboxClaimClient(custom_objects, Mock(), "agent-workspaces")

    assert await client.renew("task-one", datetime(2026, 8, 1, 5, 0, tzinfo=UTC))

    custom_objects.patch_namespaced_custom_object.assert_awaited_once()
    patch = custom_objects.patch_namespaced_custom_object.await_args.args[5]
    assert patch == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "9"},
        {"op": "replace", "path": "/spec/lifecycle/shutdownTime", "value": "2026-08-01T05:00:00Z"},
    ]


async def test_shared_claim_client_graph_snapshots_claim_sandbox_and_pod() -> None:
    custom_objects = Mock()
    custom_objects.get_namespaced_custom_object = AsyncMock(
        side_effect=[
            {"status": {"sandbox": {"name": "sandbox-one"}}},
            {"metadata": {"annotations": {"agents.x-k8s.io/pod-name": "pod-one"}}},
        ]
    )
    pod = object()
    core_v1 = Mock()
    core_v1.read_namespaced_pod = AsyncMock(return_value=pod)

    graph = await SandboxClaimClient(custom_objects, core_v1, "agent-workspaces").graph("claim-one")

    assert graph.claim is not None
    assert graph.sandbox_name == "sandbox-one"
    assert graph.sandbox is not None
    assert graph.pod_name == "pod-one"
    assert graph.pod is pod


@pytest.mark.parametrize("case", ["claim", "sandbox", "pod"])
async def test_shared_claim_client_graph_keeps_missing_resources_explicit(case: str) -> None:
    custom_objects = Mock()
    core_v1 = Mock()
    claim = {"status": {"sandbox": {"name": "sandbox-one"}}}
    sandbox = {"metadata": {"annotations": {}}}
    if case == "claim":
        custom_objects.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404))
    elif case == "sandbox":
        custom_objects.get_namespaced_custom_object = AsyncMock(
            side_effect=[claim, ApiException(status=404)]
        )
    else:
        custom_objects.get_namespaced_custom_object = AsyncMock(
            side_effect=[claim, sandbox]
        )
        core_v1.read_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))

    graph = await SandboxClaimClient(custom_objects, core_v1, "agent-workspaces").graph("claim-one")

    if case == "claim":
        assert graph.claim is None
    elif case == "sandbox":
        assert graph.claim == claim
        assert graph.sandbox_name == "sandbox-one"
        assert graph.sandbox is None
    else:
        assert graph.sandbox is sandbox
        assert graph.pod_name == "sandbox-one"
        assert graph.pod is None


if __name__ == "__main__":
    pytest_bazel.main()
