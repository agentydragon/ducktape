"""The operator's Sandbox writes, made by the app through Kubernetes."""

from typing import cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.app.sandbox_access import SandboxAccess
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest
from agentplane.sandbox_resource import SANDBOXES_PLURAL, OperatingMode
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.sandboxes.errors import InvalidSandboxError, SandboxNotFoundError, UnknownPolicySetError
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


@pytest.fixture(autouse=True)
async def close_fake_clients():
    yield
    await close_fake_api_clients()


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    objects = FakeCustomObjectsApi()
    objects.objects[(POLICY_SETS_PLURAL, "test-default")] = {"metadata": {"name": "test-default"}}
    return objects


@pytest.fixture
def access(custom_objects: FakeCustomObjectsApi) -> SandboxAccess:
    return SandboxAccess(
        SandboxInventory(
            namespace=NAMESPACE,
            custom_objects=cast(CustomObjectsClient, custom_objects),
            core_v1=cast(k8s_client.CoreV1Api, FakeCoreV1Api()),
        ),
        default_policy_sets={"test-default"},
        caller=ServiceAccountRef(namespace=NAMESPACE, name="test-app"),
    )


async def test_create_binds_the_default_policy_sets(
    access: SandboxAccess, custom_objects: FakeCustomObjectsApi
) -> None:
    view = await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))

    assert view.operating_mode == OperatingMode.RUNNING
    assert custom_objects.objects[(SANDBOXES_PLURAL, "my-task")]["spec"]["policySets"] == ["test-default"]
    assert (await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))).uid == view.uid


async def test_create_refuses_an_invalid_request_and_an_unknown_policy_set(access: SandboxAccess) -> None:
    with pytest.raises(InvalidSandboxError):
        await access.create(CreateSandboxRequest(name="Not_Valid", template=TEMPLATE))
    with pytest.raises(UnknownPolicySetError):
        await access.create(CreateSandboxRequest(name="needs-more", template=TEMPLATE, policy_sets=["test-missing"]))


async def test_lifecycle_acts_on_the_incarnation_it_read(access: SandboxAccess) -> None:
    await access.create(CreateSandboxRequest(name="my-task", template=TEMPLATE))

    await access.suspend("my-task")
    assert (await access.get("my-task")).operating_mode == OperatingMode.SUSPENDED
    await access.resume("my-task")
    assert (await access.get("my-task")).operating_mode == OperatingMode.RUNNING
    with pytest.raises(SandboxNotFoundError):
        await access.suspend("never-made")


if __name__ == "__main__":
    pytest_bazel.main()
