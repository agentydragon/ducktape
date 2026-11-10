"""Selectable Pod cases sharing one stack and two concurrent case slots."""

from collections.abc import AsyncIterator
from datetime import timedelta
from functools import partial
from typing import cast

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.case_batch import CASE_RESULT, Case, CaseBatch
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites import pod_runners
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Resources, Scenario
from util.net import Listener

pytestmark = [pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]
CASE_LIMIT = 2


@pytest.fixture(scope="session")
def token(deployment: Stack) -> str:
    """This suite calls the app as the fixture's test operator, with an ID token the test signs."""
    assert deployment.operator_tokens is not None
    # The suite's `enormous` timeout; the agent ServiceAccount token this replaces also lasts an hour.
    return deployment.operator_tokens.mint(lifetime=timedelta(hours=1))


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
        selected_cases: dict[str, Case] = {}
        for item in request.session.items:
            if isinstance(item, pytest.Function) and item.parent is request.node:
                selected = cast(Scenario, item.callspec.params["case"])
                selected_cases[selected.__name__] = Case(nodeid=item.nodeid, run=partial(selected, resources))
        storage = "fresh_workspace_storage_survives_replacement"
        async with CaseBatch.running(
            selected_cases, limit=CASE_LIMIT, exclusive=storage if storage in selected_cases else None
        ) as batch:
            yield batch


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, pod_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    result = await pod_cases.result(case.__name__)
    request.node.stash[CASE_RESULT] = result
    if result.error is not None:
        raise result.error


if __name__ == "__main__":
    pytest_bazel.main()
