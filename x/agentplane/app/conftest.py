"""Fixtures over the fake Kubernetes in `testing/kubernetes.py`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.testing.kubernetes import NAMESPACE, WARM_POOL, FakeCoreV1Api, FakeCustomObjectsApi


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(
        namespace=NAMESPACE, warm_pool=WARM_POOL, custom_objects=cast(Any, custom_objects), core_v1=cast(Any, core_v1)
    )
