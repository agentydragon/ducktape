"""MCP Actions cases sharing one stack with bounded case-owned work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

import pytest
import pytest_asyncio
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import mcp_servers
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.case_batch import CASE_RESULT, Case, CaseBatch, Result
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.harness_pair import run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    operator_bff_client,
    read_operator_credentials,
)
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import mcp
from util.net import Listener

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class ActionResources:
    cluster: Cluster
    base_url: str
    token: str
    scripted: ScriptedUpstream
    tmp_path_factory: pytest.TempPathFactory

    @asynccontextmanager
    async def open_case(self, name: str) -> AsyncIterator[tuple[Client, SessionDispatcherClient, Sandboxes]]:
        async with (
            self.scripted.case_scope(),
            scenario.open_resources(self.cluster, self.base_url, self.token, tracer, native_diagnostics=False) as (
                client,
                sandboxes,
            ),
            native_client(
                self.cluster, self.tmp_path_factory.mktemp(f"mcp-actions-{name}") / "dispatcher-token"
            ) as dispatcher,
        ):
            yield client, dispatcher, sandboxes


@asynccontextmanager
async def _policy_objects(cluster: Cluster) -> AsyncIterator[mcp.PolicyObjects]:
    objects = mcp.PolicyObjects(cluster=cluster)
    try:
        yield objects
    finally:
        closing = asyncio.create_task(objects.delete_all())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise


async def _executes_action(resources: ActionResources) -> None:
    async with (
        resources.open_case("executes") as (client, dispatcher, _),
        _policy_objects(resources.cluster) as policies,
    ):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, offered_model, sandbox: mcp.agent_executes_mcp_action(
                client,
                dispatcher,
                sandbox,
                harness,
                offered_model,
                resources.scripted,
                policies,
                "systest-actions-executes",
            ),
        )


async def _bff_decision(resources: ActionResources, credentials: OperatorCredentials) -> None:
    async with (
        resources.open_case("bff") as (client, dispatcher, _),
        operator_bff_client(resources.base_url, credentials) as operator_bff,
    ):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, offered_model, sandbox: mcp.agent_mcp_bff_decision(
                operator_bff,
                credentials,
                client,
                dispatcher,
                sandbox,
                harness,
                offered_model,
                resources.scripted,
                "systest-actions-bff",
            ),
        )


async def _launcher_ownership(resources: ActionResources, credentials: OperatorCredentials) -> None:
    async with (
        resources.open_case("launcher") as (client, dispatcher, _),
        operator_bff_client(resources.base_url, credentials) as operator_bff,
        _policy_objects(resources.cluster) as policies,
    ):
        await run_harness_pair_in_own_sandboxes(
            resources.cluster,
            client,
            lambda harness, offered_model, sandbox: mcp.launcher_drives_only_the_sandbox_it_launched(
                resources.cluster,
                operator_bff,
                client,
                dispatcher,
                sandbox,
                harness,
                offered_model,
                resources.scripted,
                policies,
                "systest-actions-launcher",
            ),
        )


async def _oauth_linkage(
    resources: ActionResources, credentials: OperatorCredentials, harness: session_pb2.Harness
) -> None:
    server_id = {
        session_pb2.HARNESS_CLAUDE: mcp_servers.OAUTH_CLAUDE,
        session_pb2.HARNESS_CODEX: mcp_servers.OAUTH_CODEX,
    }[harness]
    async with (
        resources.open_case(server_id) as (client, dispatcher, sandboxes),
        operator_bff_client(resources.base_url, credentials) as operator_bff,
        _policy_objects(resources.cluster) as policies,
    ):
        models = await models_offered_for(client, [harness])
        await mcp.operator_links_oauth_mcp_server(
            operator_bff,
            credentials,
            client,
            dispatcher,
            sandboxes,
            harness,
            models[harness],
            resources.scripted,
            policies,
            server_id=server_id,
        )


def _case_runner(
    name: str, resources: ActionResources, credentials: OperatorCredentials | None
) -> Callable[[], Awaitable[None]]:
    match name:
        case "test_agent_executes_mcp_action":
            return partial(_executes_action, resources)
        case "test_agent_mcp_bff_decision":
            if credentials is None:
                raise RuntimeError("the BFF decision case requires operator credentials")
            return partial(_bff_decision, resources, credentials)
        case "test_launcher_drives_only_the_sandbox_it_launched":
            if credentials is None:
                raise RuntimeError("the launcher case requires operator credentials")
            return partial(_launcher_ownership, resources, credentials)
        case "test_operator_links_oauth_mcp_server[claude]" | "test_operator_links_oauth_mcp_server[codex]":
            if credentials is None:
                raise RuntimeError("the OAuth linkage case requires operator credentials")
            harness = session_pb2.HARNESS_CLAUDE if name.endswith("[claude]") else session_pb2.HARNESS_CODEX
            return partial(_oauth_linkage, resources, credentials, harness)
        case _:
            raise ValueError(f"unsupported MCP Actions case: {name}")


_BATCHED_CASES = {
    "test_agent_executes_mcp_action",
    "test_agent_mcp_bff_decision",
    "test_launcher_drives_only_the_sandbox_it_launched",
    "test_operator_links_oauth_mcp_server[claude]",
    "test_operator_links_oauth_mcp_server[codex]",
}
_OPERATOR_CASES = _BATCHED_CASES - {"test_agent_executes_mcp_action"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def action_cases(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    cluster: Cluster,
    base_url: str,
    token: str,
    deployment: Stack,
    scripted_listener: Listener,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[CaseBatch]:
    selected = [
        item
        for item in request.session.items
        if isinstance(item, pytest.Function) and item.parent is request.node and item.name in _BATCHED_CASES
    ]
    if not selected:
        raise RuntimeError("the MCP Actions batch has no selected cases")

    needs_operator = any(item.name in _OPERATOR_CASES for item in selected)
    credentials = None
    if needs_operator:
        if pytestconfig.option.showlocals:
            pytest.fail(
                "BLOCKED: disable pytest local-variable dumps before reading operator credentials", pytrace=False
            )
        if deployment.mcp_fixtures is None:
            raise ValueError("operator login requires MCP fixtures in this deployment")
        try:
            credentials = read_operator_credentials(
                deployment.cluster, secret_name=deployment.mcp_fixtures.operator_secret
            )
        except LoginBlockedError as exc:
            pytest.fail(str(exc), pytrace=False)

    scripted = ScriptedUpstream()
    resources = ActionResources(
        cluster=cluster, base_url=base_url, token=token, scripted=scripted, tmp_path_factory=tmp_path_factory
    )
    cases = {
        item.name: Case(nodeid=item.nodeid, run=_case_runner(item.name, resources, credentials)) for item in selected
    }

    batch: CaseBatch | None = None
    previous_logging = logging.root.manager.disable
    if needs_operator:
        logging.disable(logging.CRITICAL)
    try:
        async with (
            scripted.serving(listener=scripted_listener.socket),
            CaseBatch.running(cases, limit=2, exclusive=None) as running,
        ):
            await asyncio.gather(*(running.result(name) for name in cases))
            batch = running
    finally:
        if needs_operator:
            logging.disable(previous_logging)

    assert batch is not None
    yield batch


async def _report_case(name: str, cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    result: Result = await cases.result(name)
    request.node.stash[CASE_RESULT] = result
    if result.error is not None:
        raise result.error


@pytest.mark.scripted_model
@pytest.mark.case_batch
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_executes_mcp_action(action_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await _report_case("test_agent_executes_mcp_action", action_cases, request)


@pytest.mark.scripted_model
@pytest.mark.case_batch
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_mcp_bff_decision(action_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await _report_case("test_agent_mcp_bff_decision", action_cases, request)


@pytest.mark.scripted_model
@pytest.mark.case_batch
@pytest.mark.asyncio(loop_scope="module")
async def test_launcher_drives_only_the_sandbox_it_launched(
    action_cases: CaseBatch, request: pytest.FixtureRequest
) -> None:
    await _report_case("test_launcher_drives_only_the_sandbox_it_launched", action_cases, request)


# Each harness owns its server linkage and Sandbox. The same two case slots bound
# all five cases; the launcher may briefly hold a parent and child (three total).
@pytest.mark.scripted_model
@pytest.mark.case_batch
@pytest.mark.asyncio(loop_scope="module")
async def test_operator_links_oauth_mcp_server(
    action_cases: CaseBatch, request: pytest.FixtureRequest, harness: session_pb2.Harness
) -> None:
    await _report_case(request.node.name, action_cases, request)


if __name__ == "__main__":
    pytest_bazel.main()
