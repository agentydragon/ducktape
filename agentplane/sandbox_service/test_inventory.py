"""The inventory reads Sandboxes and their Pods without an app or app database."""

from copy import deepcopy

import pytest
import pytest_bazel

from agentplane.proto.runner_pb2 import HARNESS_CODEX
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxPhase, SessionDefaults
from agentplane.sandbox_resource import QUIESCED, SANDBOXES_PLURAL, SESSIONS_SEALED, OperatingMode
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf


async def test_read_sandbox_without_mutation(cluster: Cluster) -> None:
    before = deepcopy(cluster.fake.objects)
    view = await cluster.inventory.get(SANDBOX)
    assert view.uid == SANDBOX_UID
    assert view.service_account.namespace == SANDBOX_NAMESPACE
    assert view.service_account.name == ACCOUNT
    assert view.operating_mode == OperatingMode.RUNNING
    assert view.HasField("pod")
    assert view.pod.name == SANDBOX
    assert view.pod.namespace == SANDBOX_NAMESPACE
    assert view.pod.uid == "test-pod-uid"
    assert cluster.fake.objects == before


async def test_sandbox_is_read_without_its_pod(cluster: Cluster) -> None:
    view = await cluster.inventory.get_without_pod(SANDBOX)
    assert view.uid == SANDBOX_UID
    assert view.service_account.name == ACCOUNT
    assert not view.HasField("pod")
    with pytest.raises(SandboxNotFoundError):
        await cluster.inventory.get_without_pod("test-missing")


async def test_sandbox_is_read_with_its_session_defaults(cluster: Cluster) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"] |= {
        "sessionDefaults": {"harness": "HARNESS_CODEX", "model": "test-model", "setupScript": "printf setup"},
        "bootstrap": "printf boot",
    }
    view = await cluster.inventory.get(SANDBOX)
    assert view.binding == SandboxBinding(
        session_defaults=SessionDefaults(harness=HARNESS_CODEX, model="test-model", setup_script="printf setup"),
        bootstrap="printf boot",
    )
    assert [sandbox.name for sandbox in await cluster.inventory.list_sandboxes()] == [SANDBOX]


async def test_suspended_sandbox_is_not_resumed(cluster: Cluster) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] = False
    cluster.fake.pods.clear()
    before = deepcopy(cluster.fake.objects)
    view = await cluster.inventory.get(SANDBOX)
    assert view.operating_mode == OperatingMode.SUSPENDED
    assert view.uid == SANDBOX_UID
    assert cluster.fake.objects == before


async def test_missing_sandbox_is_not_created(cluster: Cluster) -> None:
    before = deepcopy(cluster.fake.objects)
    with pytest.raises(SandboxNotFoundError):
        await cluster.inventory.get("test-missing")
    assert cluster.fake.objects == before


SEALED = [
    {
        "type": SESSIONS_SEALED,
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
        (False, True, False, running_because("Sealing"), SandboxPhase.SANDBOX_PHASE_QUIESCING),
        (False, True, True, running_because("Sealing"), SandboxPhase.SANDBOX_PHASE_QUIESCING),
        (False, True, False, running_because("Quiescing"), SandboxPhase.SANDBOX_PHASE_QUIESCING),
        (False, False, True, [{**SEALED[0], "type": QUIESCED}], SandboxPhase.SANDBOX_PHASE_TEARING_DOWN),
        (False, True, True, running_because("Deleting"), SandboxPhase.SANDBOX_PHASE_TEARING_DOWN),
    ],
)
async def test_phase_follows_what_the_controller_records(
    cluster: Cluster,
    running: bool,
    incarnated: bool,
    deletion: bool,
    conditions: list[dict[str, str]],
    phase: SandboxPhase,
) -> None:
    """While it seals before deletion it takes no new work but its Sessions stay readable; once sealed it is going."""
    stored = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    stored["spec"]["running"] = running
    if not incarnated:
        del stored["status"]["incarnation"]
    if deletion:
        stored["metadata"] |= {"deletionTimestamp": "2026-09-01T12:00:00Z", "finalizers": ["test-finalizer"]}
    stored["status"]["conditions"] = conditions
    assert (await cluster.inventory.get(SANDBOX)).phase == phase


if __name__ == "__main__":
    pytest_bazel.main()
