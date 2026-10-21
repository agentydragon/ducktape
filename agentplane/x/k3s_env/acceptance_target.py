"""A pytest plugin that runs acceptance scenarios against a k3s stack of their own.

An acceptance scenario's `_k3s` and `_k3s_vm` targets load it by name (`PYTEST_PLUGINS`). Before any
scenario fixture runs, it starts the stack and publishes it the way an operator names a deployment:
the `AGENTPLANE_ACCEPTANCE_*` variables the acceptance `conftest` reads, `KUBECONFIG`, and `kubectl`
on `PATH`. LLM ingress forwards to the scenario's scripted model on the node, which the scenario starts
on the published address (`scripted_upstream`). A stack with the MCP fixtures also publishes the
operator's login Secret for `operator_login` and the Action Service's MCP endpoint, and trusts Dex's CA
(`SSL_CERT_FILE`).
"""

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentplane.acceptance import operator_login, scripted_upstream
from agentplane.acceptance.conftest import ACTIONS_MCP_URL, BASE_URL, NAMESPACE, TOKEN
from agentplane.x.k3s_env import rendered_fixture
from agentplane.x.k3s_env.stack import node_ip, running_stack
from util.bazel.runfiles import get_required_path


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A stack rendered with VM Sandboxes runs the `vm_sandboxes` scenarios and only those; the other
    target runs the rest on a stack without KubeVirt."""
    vm_stack = rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"])).vm_sandboxes
    other = [item for item in items if (item.get_closest_marker("vm_sandboxes") is not None) != vm_stack]
    config.hook.pytest_deselected(items=other)
    items[:] = [item for item in items if item not in other]


@pytest.fixture(scope="session", autouse=True)
def k3s_deployment() -> Iterator[None]:
    host = node_ip()
    # A free port now; each scenario listens on it while it runs. Nothing else binds ports in this action.
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    with (
        running_stack(models=rendered_fixture.LocalModel(url=f"http://{host}:{port}")) as stack,
        tempfile.TemporaryDirectory() as tools,
        pytest.MonkeyPatch.context() as environment,
    ):
        # k3s is a multi-call binary: invoked as `kubectl`, it is a kubectl of the server's own version.
        (Path(tools) / "kubectl").symlink_to(stack.k3s.binary)
        environment.setenv("PATH", tools, prepend=os.pathsep)
        environment.setenv("KUBECONFIG", str(stack.k3s.kubeconfig))
        environment.setenv(BASE_URL, stack.base_url)
        environment.setenv(TOKEN, stack.token)
        environment.setenv(NAMESPACE, stack.namespace)
        environment.setenv(scripted_upstream.ADDRESS, f"{host}:{port}")
        if stack.mcp_fixtures is not None:
            environment.setenv("SSL_CERT_FILE", str(stack.mcp_fixtures.ca_bundle))
            environment.setenv(
                operator_login.SECRET_PATH_ENV,
                f"/api/v1/namespaces/{stack.namespace}/secrets/{stack.mcp_fixtures.operator_secret}",
            )
            environment.setenv(ACTIONS_MCP_URL, stack.mcp_fixtures.actions_mcp_url)
        yield
