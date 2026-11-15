"""Selectable Pod cases sharing one stack and two concurrent case slots."""

from collections.abc import AsyncIterator
from functools import partial
from typing import cast

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites import pod_runners
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Resources, Scenario
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)

pytestmark = [pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]
CASE_LIMIT = 2


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pod_cases(
    request: pytest.FixtureRequest,
    cluster: Cluster,
    base_url: str,
    token: str,
    scripted_listener: Listener,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[CaseBatch]:
    upstream = ScriptedUpstream()
    async with (
        native_client(cluster, tmp_path_factory.mktemp("pod-dispatcher") / "token") as dispatcher,
        upstream.serving(listener=scripted_listener.socket),
        pod_runners.pool(cluster, base_url, token, limit=CASE_LIMIT) as borrow,
    ):
        resources = Resources(
            cluster=cluster, base_url=base_url, token=token, scripted=upstream, dispatcher=dispatcher, borrow=borrow
        )
        cases = {
            item.nodeid: Case.for_item(item, partial(cast(Scenario, item.callspec.params["case"]), resources))
            for item in selected_items(request)
        }
        async with CaseBatch.running(cases, limit=CASE_LIMIT) as batch:
            yield batch


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, pod_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await pod_cases.report(request)


if __name__ == "__main__":
    pytest_bazel.main()
