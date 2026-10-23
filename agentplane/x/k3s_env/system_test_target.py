"""A pytest plugin that runs system test scenarios against a k3s stack of their own.

A scenario's k3s and `_vm` targets load it by name (`PYTEST_PLUGINS`). Before any
scenario fixture runs, it starts the stack and publishes it the way an operator names a deployment:
the `AGENTPLANE_SYSTEM_TEST_*` variables the system tests' `conftest` reads, `KUBECONFIG`, and `kubectl`
on `PATH`. LLM ingress forwards to the scenario's scripted model on the node, which the scenario starts
on the published address (`scripted_upstream`). A stack with the MCP fixtures also publishes the
operator's login Secret for `operator_login` and the standalone MCP edge endpoint, and trusts Dex's CA
(`SSL_CERT_FILE`). The test log ends with the stack's setup timeline, how long each step and each image
import took.
"""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentplane.system_tests import operator_login
from agentplane.system_tests.conftest import BASE_URL, MCP_EDGE_URL, NAMESPACE, TOKEN
from agentplane.x.k3s_env import rendered_fixture
from agentplane.x.k3s_env.stack import Timeline, node_ip, running_stack
from util.bazel.runfiles import get_required_path
from util.net import Listener, listen_tcp

TIMELINE = pytest.StashKey[Timeline]()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A stack rendered with VM Sandboxes runs the `vm_sandboxes` scenarios and only those; the other
    target runs the rest on a stack without KubeVirt."""
    vm_stack = rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"])).vm_sandboxes
    other = [item for item in items if (item.get_closest_marker("vm_sandboxes") is not None) != vm_stack]
    config.hook.pytest_deselected(items=other)
    items[:] = [item for item in items if item not in other]


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    # Absent when no scenario was collected, so the stack never started.
    if (timeline := config.stash.get(TIMELINE, None)) is not None:
        terminalreporter.write_sep("-", "k3s stack setup")
        terminalreporter.write_line(timeline.summary())


@pytest.fixture(scope="session")
def scripted_listener() -> Iterator[Listener]:
    """Retain the scripted model's binding until the deployment and all scenarios stop."""
    with listen_tcp(host=node_ip()) as listener:
        yield listener


@pytest.fixture(scope="session", autouse=True)
def k3s_deployment(pytestconfig: pytest.Config, scripted_listener: Listener) -> Iterator[None]:
    timeline = pytestconfig.stash[TIMELINE] = Timeline()
    with (
        running_stack(
            models=rendered_fixture.LocalModel(url=f"http://{scripted_listener.host}:{scripted_listener.port}"),
            timeline=timeline,
        ) as stack,
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
        if stack.mcp_fixtures is not None:
            environment.setenv("SSL_CERT_FILE", str(stack.mcp_fixtures.ca_bundle))
            environment.setenv(
                operator_login.SECRET_PATH_ENV,
                f"/api/v1/namespaces/{stack.namespace}/secrets/{stack.mcp_fixtures.operator_secret}",
            )
            environment.setenv(MCP_EDGE_URL, stack.mcp_fixtures.mcp_edge_url)
        yield
