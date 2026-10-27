"""The inventory reads Sandboxes and their Pods without an app or app database."""

from collections.abc import AsyncIterator

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.crd_group import GROUP, VERSION
from agentplane.proto.sandboxes_pb2 import SandboxBinding, SandboxPhase
from agentplane.sandbox_resource import QUIESCED, SANDBOXES_PLURAL, OperatingMode
from agentplane.sandboxes.errors import SandboxNotFoundError
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster, sandbox_cluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
async def cluster(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    """A namespace of its own on the target's control plane, which the conftest starts once."""
    async with sandbox_cluster(api_server) as started:
        yield started


async def stored(cluster: SandboxCluster) -> set[tuple[str, str, str]]:
    """Every Sandbox and Pod in the cluster's namespace at its resourceVersion, which any write to it moves."""
    sandboxes = await cluster.custom_objects.list_namespaced_custom_object(
        GROUP, VERSION, cluster.namespace, SANDBOXES_PLURAL
    )
    pods = await k8s_client.CoreV1Api(cluster.server.api).list_namespaced_pod(cluster.namespace)
    return {
        *(
            (SANDBOXES_PLURAL, item["metadata"]["name"], item["metadata"]["resourceVersion"])
            for item in sandboxes["items"]
        ),
        *(("pods", pod.metadata.name, pod.metadata.resource_version) for pod in pods.items),
    }


async def test_read_sandbox_without_mutation(cluster: SandboxCluster) -> None:
    before = await stored(cluster)
    view = await cluster.inventory.get(SANDBOX)
    assert view.uid == cluster.uid
    assert view.service_account.namespace == cluster.namespace
    assert view.service_account.name == SANDBOX
    assert view.operating_mode == OperatingMode.RUNNING
    assert view.HasField("pod")
    assert view.pod.name == SANDBOX
    assert view.pod.namespace == cluster.namespace
    assert view.pod.uid == cluster.pod_uid
    assert await stored(cluster) == before


async def test_sandbox_is_read_without_its_pod(cluster: SandboxCluster) -> None:
    view = await cluster.inventory.get_without_pod(SANDBOX)
    assert view.uid == cluster.uid
    assert view.service_account.name == SANDBOX
    assert not view.HasField("pod")
    with pytest.raises(SandboxNotFoundError):
        await cluster.inventory.get_without_pod("test-missing")


async def test_sandbox_is_read_with_its_bootstrap(cluster: SandboxCluster) -> None:
    await cluster.patch_sandbox({"spec": {"bootstrap": "printf boot"}})
    view = await cluster.inventory.get(SANDBOX)
    assert view.binding == SandboxBinding(bootstrap="printf boot")
    assert [sandbox.name for sandbox in await cluster.inventory.list_sandboxes()] == [SANDBOX]


async def test_sandbox_inventory_projects_idle_suspend_blockers(cluster: SandboxCluster) -> None:
    await cluster.patch_sandbox_status(
        {
            "status": {
                "idleSuspendBlockers": [{"reason": "PendingCommand", "sessionId": "session-a"}, {"reason": "Bootstrap"}]
            }
        }
    )

    view = await cluster.inventory.get(SANDBOX)

    assert [(blocker.reason, blocker.session_id) for blocker in view.autosuspend_blockers] == [
        ("PendingCommand", "session-a"),
        ("Bootstrap", None),
    ]


async def test_suspended_sandbox_is_not_resumed(cluster: SandboxCluster) -> None:
    await cluster.patch_sandbox({"spec": {"running": False}})
    await cluster.patch_sandbox_status({"status": {"operatingMode": OperatingMode.SUSPENDED.value}})
    await k8s_client.CoreV1Api(cluster.server.api).delete_namespaced_pod(
        SANDBOX, cluster.namespace, body=k8s_client.V1DeleteOptions(grace_period_seconds=0)
    )
    before = await stored(cluster)
    view = await cluster.inventory.get(SANDBOX)
    assert view.operating_mode == OperatingMode.SUSPENDED
    assert view.uid == cluster.uid
    assert await stored(cluster) == before


async def test_missing_sandbox_is_not_created(cluster: SandboxCluster) -> None:
    before = await stored(cluster)
    with pytest.raises(SandboxNotFoundError):
        await cluster.inventory.get("test-missing")
    assert await stored(cluster) == before


SEALED = [
    {
        "type": QUIESCED,
        "status": "True",
        "reason": "Sealed",
        "message": "",
        "lastTransitionTime": "2026-09-01T12:00:00Z",
    }
]


def running_because(reason: str) -> list[dict[str, str]]:
    return [{**SEALED[0], "type": "Running", "status": "False", "reason": reason}]


@pytest.mark.parametrize(
    ("running", "incarnated", "deletion", "conditions", "phase"),
    [
        (True, False, False, [], SandboxPhase.SANDBOX_PHASE_CREATING),
        (True, True, False, [], SandboxPhase.SANDBOX_PHASE_RUNNING),
        (False, True, False, [], SandboxPhase.SANDBOX_PHASE_SUSPENDING),
        (False, False, False, [], SandboxPhase.SANDBOX_PHASE_SUSPENDED),
        (False, True, True, [], SandboxPhase.SANDBOX_PHASE_QUIESCING),
        (False, False, True, SEALED, SandboxPhase.SANDBOX_PHASE_TEARING_DOWN),
        (False, True, False, running_because("Quiescing"), SandboxPhase.SANDBOX_PHASE_QUIESCING),
        (False, True, True, running_because("Deleting"), SandboxPhase.SANDBOX_PHASE_TEARING_DOWN),
    ],
)
async def test_phase_follows_what_the_controller_records(
    cluster: SandboxCluster,
    running: bool,
    incarnated: bool,
    deletion: bool,
    conditions: list[dict[str, str]],
    phase: SandboxPhase,
) -> None:
    """While it seals before deletion it takes no new work but its Sessions stay readable; once sealed it is going."""
    await cluster.patch_sandbox({"spec": {"running": running}})
    await cluster.patch_sandbox_status(
        {
            "status": {
                "operatingMode": (OperatingMode.RUNNING if running else OperatingMode.SUSPENDED).value,
                "conditions": conditions,
                **({} if incarnated else {"incarnation": None}),
            }
        }
    )
    if deletion:
        # The finalizer holds the deleted Sandbox, as the Sandbox Controller's does while it seals.
        await cluster.patch_sandbox({"metadata": {"finalizers": ["test.invalid/hold"]}})
        await cluster.custom_objects.delete_namespaced_custom_object(
            GROUP, VERSION, cluster.namespace, SANDBOXES_PLURAL, SANDBOX, body=k8s_client.V1DeleteOptions()
        )
    assert (await cluster.inventory.get(SANDBOX)).phase == phase


async def test_initial_sandbox_without_controller_status_is_unknown(cluster: SandboxCluster) -> None:
    await cluster.patch_sandbox_status({"status": {"operatingMode": None, "incarnation": None}})
    view = await cluster.inventory.get(SANDBOX)

    assert not view.HasField("operating_mode")
    assert view.phase == SandboxPhase.SANDBOX_PHASE_UNSPECIFIED
    assert not view.HasField("pod")


if __name__ == "__main__":
    pytest_bazel.main()
