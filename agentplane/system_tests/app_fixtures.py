"""Explicit App clients and lifecycle fixtures for suites that exercise the App."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from functools import partial
from typing import cast

import pytest
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import native_artifacts, scenario
from agentplane.system_tests.driver.app_sandbox import sandbox_scope
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.ollama_warmup import warm_ollama
from agentplane.system_tests.driver.sandbox import Sandboxes

# Generated Session annotations import protobuf during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if call.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


@pytest.fixture
async def dispatcher(cluster: Cluster) -> AsyncIterator[SessionDispatcherClient]:
    """Native Dispatcher client using an authorized App service SVID."""
    async with native_client(cluster) as client:
        yield client


@pytest.fixture(params=[session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> session_pb2.Harness:
    """Every scenario runs on every harness: one runner protocol, so one test body covers both."""
    return cast(session_pb2.Harness, request.param)


@pytest.fixture
async def client(
    base_url: str,
    token: str,
    request: pytest.FixtureRequest,
    phase_tracer: trace.Tracer,
    dispatcher: SessionDispatcherClient,
) -> AsyncIterator[Client]:
    async with Client.connect(base_url=base_url, token=token, dispatcher=dispatcher) as opened:
        try:
            yield opened
        finally:
            report = request.node.stash.get(_CALL_REPORT, None)
            if request.node.get_closest_marker("vm_sandboxes") is not None or (report is not None and report.failed):
                for uid in opened.opened_sandbox_uids:
                    with phase_tracer.start_as_current_span(
                        "system_test.diagnostics", attributes={"sandbox.uid": str(uid)}
                    ):
                        await native_artifacts.capture(opened, sandbox_uid=uid)


@pytest.fixture
async def offered_model(client: Client, harness: session_pb2.Harness, phase_tracer: trace.Tracer) -> str:
    """A model this deployment offers for this harness, asked of the app rather than hardcoded."""
    with phase_tracer.start_as_current_span(
        "system_test.model.discovery", record_exception=False, set_status_on_exception=False
    ):
        offered = await models_offered_for(client, [harness])
    return offered[harness]


@pytest.fixture
async def model(offered_model: str, phase_tracer: trace.Tracer) -> str:
    """`offered_model`, loaded first if Ollama serves it: a real model's turn does not wait out a cold load."""
    with phase_tracer.start_as_current_span(
        "system_test.model.warmup", record_exception=False, set_status_on_exception=False
    ):
        await warm_ollama(offered_model)
    return offered_model


@pytest.fixture
def scenario_resources(
    cluster: Cluster, base_url: str, token: str, phase_tracer: trace.Tracer
) -> scenario.ScenarioResources:
    """Each use owns its client and guests; the deployment and scripted server stay shared."""
    return partial(scenario.open_resources, cluster, base_url, token, phase_tracer)


@pytest.fixture
async def sandbox(cluster: Cluster, client: Client, phase_tracer: trace.Tracer) -> AsyncIterator[Sandboxes]:
    """Creates sandboxes and deletes every one it created, whatever the test did.

    Deletion is refused while a sandbox runs, so each is suspended first. A sandbox that will not go
    fails the teardown rather than being swallowed: a leaked one holds a PVC and a node slot. A run
    killed outright still leaks — see the sweeper entry in this package's README.
    """
    async with sandbox_scope(cluster, client, phase_tracer) as create:
        yield create
