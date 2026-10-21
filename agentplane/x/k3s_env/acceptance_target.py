"""A pytest plugin that runs acceptance scenarios against a k3s stack of their own.

An acceptance scenario's `_k3s` target loads it by name (`PYTEST_PLUGINS`). Before any scenario
fixture runs, it starts the stack and publishes it the way an operator names a deployment: the
`AGENTPLANE_ACCEPTANCE_*` variables the acceptance `conftest` reads, `KUBECONFIG`, and `kubectl` on
`PATH`. LLM ingress forwards to the scenario's scripted model on the node, which the scenario starts
on the published address (`scripted_upstream`).
"""

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentplane.acceptance import scripted_upstream
from agentplane.acceptance.conftest import BASE_URL, NAMESPACE, TOKEN
from agentplane.x.k3s_env.rendered_fixture import LocalModel
from agentplane.x.k3s_env.stack import node_ip, running_stack


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """The stack runs no VM Sandboxes: the RBE executor has no `/dev/kvm`."""
    vm = [item for item in items if item.get_closest_marker("vm_sandboxes") is not None]
    config.hook.pytest_deselected(items=vm)
    items[:] = [item for item in items if item not in vm]


@pytest.fixture(scope="session", autouse=True)
def k3s_deployment() -> Iterator[None]:
    host = node_ip()
    # A free port now; each scenario listens on it while it runs. Nothing else binds ports in this action.
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    with (
        running_stack(models=LocalModel(url=f"http://{host}:{port}")) as stack,
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
        yield
