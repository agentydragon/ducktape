"""Sandbox readers and the inventory over a real API server, each test in a namespace of its own."""

import pytest

from agentplane.sandboxes.egress_views import EgressReader
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig


@pytest.fixture
async def namespace(api_server: KubernetesApiServer) -> SandboxNamespace:
    return await SandboxNamespace.new(api_server)


@pytest.fixture
def egress(namespace: SandboxNamespace) -> EgressReader:
    return EgressReader(namespace=namespace.name, custom_objects=namespace.custom_objects)


@pytest.fixture
def inventory(namespace: SandboxNamespace) -> SandboxInventory:
    return SandboxInventory(
        namespace=namespace.name, custom_objects=namespace.custom_objects, core_v1=namespace.core_v1
    )
