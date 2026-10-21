"""The operator's Sandbox writes, made by the app through Kubernetes."""

import pytest
import pytest_bazel

from agentplane.app.sandbox_access import SandboxAccess
from agentplane.proto.sandboxes_pb2 import CreateSandboxRequest
from agentplane.sandbox_resource import SANDBOXES_PLURAL, OperatingMode
from agentplane.sandboxes.errors import InvalidSandboxError, SandboxNotFoundError, UnknownPolicySetError
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.sandbox_namespace import TEMPLATE, SandboxNamespace

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
async def access(namespace: SandboxNamespace) -> SandboxAccess:
    await namespace.egress_policy_set("test-default", [{"hosts": ["test-default.test"]}])
    return SandboxAccess(
        SandboxInventory(namespace=namespace.name, custom_objects=namespace.custom_objects, core_v1=namespace.core_v1),
        default_policy_sets={"test-default"},
        caller=namespace.account("test-app"),
    )


async def test_create_binds_the_default_policy_sets(access: SandboxAccess, namespace: SandboxNamespace) -> None:
    view = await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))

    assert not view.HasField("operating_mode")
    assert (await namespace.read(SANDBOXES_PLURAL, "my-task"))["spec"]["policySets"] == ["test-default"]
    assert (await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))).uid == view.uid


async def test_create_refuses_an_invalid_request_and_an_unknown_policy_set(access: SandboxAccess) -> None:
    with pytest.raises(InvalidSandboxError):
        await access.create(CreateSandboxRequest(name="Not_Valid", template=TEMPLATE))
    with pytest.raises(UnknownPolicySetError):
        await access.create(CreateSandboxRequest(name="needs-more", template=TEMPLATE, policy_sets=["test-missing"]))


async def test_lifecycle_acts_on_the_incarnation_it_read(access: SandboxAccess, namespace: SandboxNamespace) -> None:
    await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))

    await access.suspend("my-task")
    assert not (await access.get("my-task")).HasField("operating_mode")
    await namespace.patch_status(SANDBOXES_PLURAL, "my-task", {"operatingMode": "Suspended"})
    assert (await access.get("my-task")).operating_mode == OperatingMode.SUSPENDED
    await access.resume("my-task")
    assert (await access.get("my-task")).operating_mode == OperatingMode.SUSPENDED
    await namespace.patch_status(SANDBOXES_PLURAL, "my-task", {"operatingMode": "Running"})
    assert (await access.get("my-task")).operating_mode == OperatingMode.RUNNING
    with pytest.raises(SandboxNotFoundError):
        await access.suspend("never-made")


if __name__ == "__main__":
    pytest_bazel.main()
