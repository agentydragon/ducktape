"""MCP Actions cases sharing one stack with bounded case-owned work."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import cast

import pytest
import pytest_asyncio

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import mcp_servers
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items
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
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import mcp
from util.net import Listener

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class ActionResources:
    cluster: Cluster
    base_url: str
    token: str
    scripted: ScriptedUpstream
    credentials: OperatorCredentials | None

    def operator_credentials(self) -> OperatorCredentials:
        if self.credentials is None:
            raise RuntimeError("this MCP Actions case requires operator credentials")
        return self.credentials

    @asynccontextmanager
    async def open_case(self) -> AsyncIterator[tuple[Client, SessionDispatcherClient, Sandboxes]]:
        async with (
            self.scripted.case_scope(),
            scenario.open_resources(self.cluster, self.base_url, self.token, tracer, native_diagnostics=False) as (
                client,
                sandboxes,
            ),
            native_client(self.cluster) as dispatcher,
        ):
            yield client, dispatcher, sandboxes


async def _executes_action(resources: ActionResources) -> None:
    async with resources.open_case() as (client, dispatcher, _), PolicyObjects(resources.cluster) as policies:
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


async def _bff_decision(resources: ActionResources) -> None:
    credentials = resources.operator_credentials()
    async with (
        resources.open_case() as (client, dispatcher, _),
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


async def _launcher_ownership(resources: ActionResources) -> None:
    credentials = resources.operator_credentials()
    async with (
        resources.open_case() as (client, dispatcher, _),
        operator_bff_client(resources.base_url, credentials) as operator_bff,
        PolicyObjects(resources.cluster) as policies,
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


async def _oauth_linkage(resources: ActionResources, *, harness: session_pb2.Harness) -> None:
    credentials = resources.operator_credentials()
    server_id = {
        session_pb2.HARNESS_CLAUDE: mcp_servers.OAUTH_CLAUDE,
        session_pb2.HARNESS_CODEX: mcp_servers.OAUTH_CODEX,
    }[harness]
    async with (
        resources.open_case() as (client, dispatcher, sandboxes),
        operator_bff_client(resources.base_url, credentials) as operator_bff,
        PolicyObjects(resources.cluster) as policies,
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


@dataclass(frozen=True)
class ActionCase:
    run: Callable[[ActionResources], Awaitable[None]]
    requires_operator: bool = True


CASES = (
    pytest.param(ActionCase(_executes_action, requires_operator=False), id="test_agent_executes_mcp_action"),
    pytest.param(ActionCase(_bff_decision), id="test_agent_mcp_bff_decision"),
    pytest.param(ActionCase(_launcher_ownership), id="test_launcher_drives_only_the_sandbox_it_launched"),
    pytest.param(
        ActionCase(partial(_oauth_linkage, harness=session_pb2.HARNESS_CLAUDE)),
        id="test_operator_links_oauth_mcp_server-claude",
    ),
    pytest.param(
        ActionCase(partial(_oauth_linkage, harness=session_pb2.HARNESS_CODEX)),
        id="test_operator_links_oauth_mcp_server-codex",
    ),
)

pytestmark = [pytest.mark.scripted_model, pytest.mark.case_batch, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def action_cases(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    cluster: Cluster,
    base_url: str,
    token: str,
    deployment: Stack,
    scripted_listener: Listener,
) -> AsyncIterator[CaseBatch]:
    selected = {item: cast(ActionCase, item.callspec.params["case"]) for item in selected_items(request)}
    needs_operator = any(case.requires_operator for case in selected.values())
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
        cluster=cluster, base_url=base_url, token=token, scripted=scripted, credentials=credentials
    )
    cases = {item.nodeid: Case.for_item(item, case.run) for item, case in selected.items()}

    previous_logging = logging.root.manager.disable
    if needs_operator:
        logging.disable(logging.CRITICAL)
    try:
        async with (
            scripted.serving(listener=scripted_listener.socket),
            CaseBatch.running(cases, limit=2, worker_scope=lambda: nullcontext(resources)) as running,
        ):
            yield running
    finally:
        if needs_operator:
            logging.disable(previous_logging)


@pytest.mark.parametrize("case", CASES)
async def test_mcp_actions(case: ActionCase, action_cases: CaseBatch, request: pytest.FixtureRequest) -> None:
    await action_cases.report(request)
