"""Fixtures over the fake Kubernetes in `testing/kubernetes.py`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.app.testing.kubernetes import NAMESPACE, WARM_POOL, FakeCoreV1Api, FakeCustomObjectsApi

# The bridge tests run one script against a local runner over both harnesses; those fixtures live
# with the runner.
from x.agentplane.runner.conftest import config, model, provider, runner, spec, upstream, workspace


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def bridge() -> RunnerBridge:
    """A bridge with nothing to dial, for the inventory routes."""

    async def unreachable(name: str) -> str:
        raise SandboxNotReachableError(name, ProvisioningState.CLAIM_CREATED)

    return RunnerBridge(address_of=unreachable)


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(
        namespace=NAMESPACE, warm_pool=WARM_POOL, custom_objects=cast(Any, custom_objects), core_v1=cast(Any, core_v1)
    )
