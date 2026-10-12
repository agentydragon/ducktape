"""The relocated inventory reads retained Kubernetes state without an app or app database."""

import json
from copy import deepcopy

import pytest
import pytest_bazel

from agentplane.proto.runner_pb2 import HARNESS_CODEX
from agentplane.proto.sandbox_service_pb2 import (
    TEARDOWN_PHASE_SEALING,
    TEARDOWN_PHASE_UNSPECIFIED,
    SandboxBinding,
    SessionDefaults,
)
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND, SESSIONS_SEALED
from agentplane.sandbox_service.binding_storage import write_binding
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, OWN_SANDBOXES, SANDBOX, SANDBOX_UID, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf


async def test_read_retained_sandbox_without_mutation(cluster: Cluster) -> None:
    before = deepcopy(cluster.fake.objects)
    view = await cluster.inventory.get(SANDBOX)
    assert view.uid == SANDBOX_UID
    assert view.service_account.namespace == SANDBOX_NAMESPACE
    assert view.service_account.name == ACCOUNT
    assert view.operating_mode == OperatingMode.RUNNING
    assert not view.launch_grants_pending
    assert view.HasField("pod")
    assert view.pod.name == SANDBOX
    assert view.pod.namespace == SANDBOX_NAMESPACE
    assert view.pod.uid == "test-pod-uid"
    assert cluster.fake.objects == before


@pytest.mark.parametrize(
    "raw",
    [
        '{"session_defaults":{"harness":"HARNESS_CODEX","model":"retained-model","cwd":"/state/{session_id}",'
        '"instructions":"","setup_script":"printf setup"},"bootstrap":"printf boot"}',
        '{"bootstrap":"printf boot"}',
    ],
)
async def test_stored_binding_is_read_without_mutation(cluster: Cluster, raw: str) -> None:
    sandbox = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    sandbox["metadata"].setdefault("annotations", {})[SANDBOX_BINDING_ANNOTATION] = raw
    before = deepcopy(cluster.fake.objects)
    view = await cluster.inventory.get(SANDBOX)
    binding = await cluster.inventory.binding(SANDBOX)
    assert binding is not None
    assert view.binding == binding
    assert json.loads(write_binding(binding)) == json.loads(raw)
    assert cluster.fake.objects == before


async def test_suspended_sandbox_is_not_resumed(cluster: Cluster) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["operatingMode"] = "Suspended"
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


_OWN = "test-own-sandbox"


def _put_own(cluster: Cluster, **metadata: object) -> None:
    cluster.fake.put(
        OWN_SANDBOXES,
        {
            "apiVersion": SANDBOX_API_VERSION,
            "kind": SANDBOX_KIND,
            "metadata": {
                "name": _OWN,
                "uid": "00000000-0000-4000-8000-0000000000aa",
                "creationTimestamp": "2026-09-01T11:00:00Z",
                **metadata,
            },
            "spec": {
                "template": "test-template",
                "running": True,
                "sessionDefaults": {"harness": "HARNESS_CODEX", "model": "test-model", "setupScript": "printf setup"},
                "bootstrap": "printf boot",
            },
        },
    )


async def test_own_sandbox_is_read_with_its_session_defaults(cluster: Cluster) -> None:
    _put_own(cluster)
    view = await cluster.inventory.get(_OWN)
    assert (view.service_account.namespace, view.service_account.name) == (SANDBOX_NAMESPACE, _OWN)
    assert view.operating_mode == OperatingMode.RUNNING
    assert view.binding == SandboxBinding(
        session_defaults=SessionDefaults(harness=HARNESS_CODEX, model="test-model", setup_script="printf setup"),
        bootstrap="printf boot",
    )
    assert {sandbox.name for sandbox in await cluster.inventory.list_sandboxes()} == {SANDBOX, _OWN}


@pytest.mark.parametrize(
    ("conditions", "teardown", "deleting"),
    [
        ([], TEARDOWN_PHASE_SEALING, False),
        (
            [
                {
                    "type": SESSIONS_SEALED,
                    "status": "True",
                    "reason": "Sealed",
                    "message": "",
                    "lastTransitionTime": "2026-09-01T12:00:00Z",
                }
            ],
            TEARDOWN_PHASE_UNSPECIFIED,
            True,
        ),
    ],
)
async def test_deleted_own_sandbox_is_in_teardown_until_sealed(
    cluster: Cluster, conditions: list[dict[str, str]], teardown: int, deleting: bool
) -> None:
    """While it seals it takes no new work but its Sessions stay readable, as an agent-sandbox one
    in teardown; once sealed it is going."""
    _put_own(cluster, deletionTimestamp="2026-09-01T12:00:00Z", finalizers=["test-finalizer"])
    cluster.fake.objects[OWN_SANDBOXES][_OWN]["status"] = {"conditions": conditions}
    view = await cluster.inventory.get(_OWN)
    assert (view.teardown, view.deleting) == (teardown, deleting)


if __name__ == "__main__":
    pytest_bazel.main()
