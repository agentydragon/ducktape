"""Manual comparison: ordinary pytest cases sharing one stack and compatible runners per shard."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites import pod_runners
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Resources, Scenario
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)
pytestmark = pytest.mark.asyncio(loop_scope="module")

# A fresh-inventory check runs before any selected scenario can create a Sandbox.
_CASES = sorted(SCENARIOS, key=lambda case: not any(mark.name == "exclusive_case" for mark in case.marks))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def resources(
    cluster: Cluster, base_url: str, token: str, scripted_listener: Listener, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Resources]:
    upstream = ScriptedUpstream()
    async with (
        native_client(cluster, tmp_path_factory.mktemp("pod-sequential-dispatcher") / "token") as dispatcher,
        upstream.serving(listener=scripted_listener.socket),
        pod_runners.Pool(cluster, base_url, token) as runner_pool,
    ):
        yield Resources(
            cluster=cluster, base_url=base_url, token=token, scripted=upstream, dispatcher=dispatcher, pool=runner_pool
        )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, resources: Resources) -> None:
    await case(resources)


if __name__ == "__main__":
    pytest_bazel.main()
