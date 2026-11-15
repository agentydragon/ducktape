"""Manual comparison: ordinary pytest cases sharing one stack and compatible runners per shard."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Scenario
from agentplane.system_tests.suites.pod_worker import Worker
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)
pytestmark = pytest.mark.asyncio(loop_scope="module")

# A fresh-inventory check runs before any selected scenario can create a Sandbox.
_CASES = sorted(SCENARIOS, key=lambda case: not any(mark.name == "exclusive_case" for mark in case.marks))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def worker(cluster: Cluster, base_url: str, token: str, scripted_listener: Listener) -> AsyncIterator[Worker]:
    upstream = ScriptedUpstream()
    async with (
        native_client(cluster) as dispatcher,
        upstream.serving(listener=scripted_listener.socket),
        Worker.open(cluster, base_url, token, upstream, dispatcher) as owned,
    ):
        yield owned


@pytest.mark.parametrize("case", _CASES, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, worker: Worker) -> None:
    await case(worker)


if __name__ == "__main__":
    pytest_bazel.main()
