"""The relocated inventory reads retained Kubernetes state without an app or app database."""

import json
from copy import deepcopy

import pytest
import pytest_bazel

from agentplane.sandbox_service.binding_storage import write_binding
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.models import OperatingMode, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, SessionDestination
from agentplane.sandbox_service.session_lifecycle import launch_spec
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.agent_sandbox import SANDBOXES_PLURAL

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
        '{"thread_defaults":{"harness":"HARNESS_CODEX","model":"retained-model","cwd":"/state/{session_id}",'
        '"instructions":"","setup_script":"printf setup"},"bootstrap":"printf boot"}',
        '{"bootstrap":"printf boot"}',
    ],
)
async def test_legacy_binding_storage_is_preserved_but_not_exposed(cluster: Cluster, raw: str) -> None:
    sandbox = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    sandbox["metadata"].setdefault("annotations", {})[SANDBOX_BINDING_ANNOTATION] = raw
    before = deepcopy(cluster.fake.objects)
    view = await cluster.inventory.get(SANDBOX)
    binding = await cluster.inventory.binding(SANDBOX)
    assert binding is not None
    assert view.binding == binding
    assert json.loads(write_binding(binding)) == json.loads(raw)
    assert binding.DESCRIPTOR.fields_by_name["session_defaults"].number == 1
    assert "thread_defaults" not in binding.DESCRIPTOR.fields_by_name
    if binding.HasField("session_defaults"):
        assert binding.session_defaults.HasField("instructions")
        assert binding.session_defaults.instructions == ""
        spec = launch_spec(
            SessionDestination(
                sandbox=SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=view.uid),
                session_id="retained-session",
            ),
            {},
            binding=binding,
            platform_instructions="",
        )
        assert spec.cwd == "/state/retained-session"
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


if __name__ == "__main__":
    pytest_bazel.main()
