"""Thirteen selectable Pod cases sharing one stack and two concurrent case slots."""

from collections.abc import AsyncIterator
from functools import partial
from typing import cast

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.system_tests import scenario
from agentplane.system_tests.case_batch import CASE_RESULT, Case, CaseBatch
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites.pod_cases import SCENARIOS, Resources, Scenario
from agentplane.system_tests.tracing import tracer
from util.net import Listener

pytestmark = [pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pod_cases(
    request: pytest.FixtureRequest, cluster: Cluster, base_url: str, token: str, scripted_listener: Listener
) -> AsyncIterator[CaseBatch]:
    upstream = ScriptedUpstream()
    resources = Resources(
        cluster=cluster,
        base_url=base_url,
        token=token,
        scripted=upstream,
        open=partial(scenario.open_resources, cluster, base_url, token, tracer, native_diagnostics=False),
    )

    async def run(selected: Scenario) -> None:
        async with upstream.case_scope():
            await selected(resources)

    selected_cases: dict[str, Case] = {}
    for item in request.session.items:
        if isinstance(item, pytest.Function) and item.parent is request.node:
            selected = cast(Scenario, item.callspec.params["case"])
            selected_cases[selected.__name__] = Case(nodeid=item.nodeid, run=partial(run, selected))
    storage = "fresh_workspace_storage_survives_replacement"
    async with (
        upstream.serving(listener=scripted_listener.socket),
        CaseBatch.running(selected_cases, limit=2, exclusive=storage if storage in selected_cases else None) as batch,
    ):
        yield batch


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: "test_" + case.__name__)
async def test_pod_scenarios(case: Scenario, pod_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    result = await pod_cases.result(case.__name__)
    request.node.stash[CASE_RESULT] = result
    if result.error is not None:
        raise result.error


if __name__ == "__main__":
    pytest_bazel.main()
