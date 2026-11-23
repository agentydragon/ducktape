"""Selectable Pod cases sharing one stack and bounded concurrent case slots."""

import os
from collections.abc import AsyncIterator
from functools import partial
from typing import cast

import pytest
import pytest_asyncio

from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Scenario
from agentplane.system_tests.suites.pod_worker import Worker
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)

pytestmark = [pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]
CASE_LIMIT = int(os.environ.get("AGENTPLANE_POD_CASE_LIMIT", "4"))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pod_cases(
    request: pytest.FixtureRequest, cluster: Cluster, base_url: str, token: str, scripted_listener: Listener
) -> AsyncIterator[CaseBatch]:
    upstream = ScriptedUpstream()
    async with native_client(cluster) as dispatcher, upstream.serving(listener=scripted_listener.socket):
        cases = {
            item.nodeid: Case.for_item(item, cast(Scenario, item.callspec.params["case"]))
            for item in selected_items(request)
        }
        async with CaseBatch.running(
            cases, limit=CASE_LIMIT, worker_scope=partial(Worker.open, cluster, base_url, token, upstream, dispatcher)
        ) as batch:
            yield batch


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, pod_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await pod_cases.report(request)
