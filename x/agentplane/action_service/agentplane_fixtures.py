"""The `everything_url` fixture, loaded via `pytest_plugins` rather than `conftest.py` --
importing cdk8s spawns jsii's Node subprocess at import time, and `conftest.py` is
auto-loaded for every test in this package (and its subpackages), most of which have
nothing to do with cdk8s. Test modules that need this fixture opt in explicitly:

    # pytest_plugins loads x.agentplane.action_service.agentplane_fixtures by name;
    # gazelle cannot see the dependency.
    # gazelle:include_dep //x/agentplane/action_service:agentplane_fixtures
    pytest_plugins = ("x.agentplane.action_service.agentplane_fixtures",)
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from cluster.cdk8s import generate_manifests


@pytest.fixture
def everything_url() -> Iterator[str]:
    documents = Cdk8sTesting.synth(generate_manifests.testing_chart(Cdk8sTesting.app()))
    deployment = one(
        doc
        for doc in documents
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "agentplane-mcp-everything"
    )
    container_spec = deployment["spec"]["template"]["spec"]["containers"][0]
    with (
        DockerContainer(container_spec["image"])
        .with_kwargs(entrypoint=container_spec["command"], read_only=True, user="1000:1000")
        .with_command([])
        .with_exposed_ports(3001) as container
    ):
        wait_for_logs(container, "MCP Streamable HTTP Server listening on port", timeout=30, raise_on_exit=True)
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(3001)}/mcp"
