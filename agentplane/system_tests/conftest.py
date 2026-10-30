"""Pytest clients, harness parameters, and Sandbox lifecycle fixtures.

K3s suites supply explicit deployment access. The separate live targets read their configuration
once at the fixture boundary and default to staging.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests import native_artifacts, scenario, scripted_upstream
from agentplane.system_tests.cluster import AGENT_SERVICE_ACCOUNT, AUDIENCE, Cluster, mint_token
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.ollama_warmup import warm_ollama
from agentplane.system_tests.sandbox import Sandboxes, sandbox_scope
from agentplane.system_tests.vm_tracing import tracer
from util.net import Listener
from util.testing.otel_tracing import configure_tracing

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

BASE_URL = "AGENTPLANE_SYSTEM_TEST_URL"
TOKEN = "AGENTPLANE_SYSTEM_TEST_TOKEN"
NAMESPACE = "AGENTPLANE_SYSTEM_TEST_NAMESPACE"
SERVICE_ACCOUNT = "AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT"


STAGING_URL = "https://agentplane-staging.allegedly.works"
STAGING_NAMESPACE = "agentplane-staging"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "vm_sandboxes: needs a deployment that runs VM Sandboxes")
    config.addinivalue_line("markers", "real_model: its evidence is what a real model does")
    config.addinivalue_line("markers", "scripted_model: the scenario plays the model (`scripted` fixture)")


def pytest_collection_finish(session: pytest.Session) -> None:
    configure_tracing(filename="vm-phases.jsonl")


@pytest.fixture
def phase_tracer(request: pytest.FixtureRequest) -> Iterator[trace.Tracer]:
    with tracer.start_as_current_span("vm.case", attributes={"pytest.nodeid": request.node.nodeid}):
        yield tracer


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> object:
    with tracer.start_as_current_span("vm.scenario"):
        return (yield)


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get(BASE_URL, STAGING_URL)


@pytest.fixture(scope="session")
def token(cluster: Cluster) -> str:
    """The caller's own token if it set one, else one minted from the caller's kubeconfig."""
    if (supplied := os.environ.get(TOKEN)) is not None:
        return supplied
    return mint_token(
        cluster=cluster, service_account=os.environ.get(SERVICE_ACCOUNT, AGENT_SERVICE_ACCOUNT), audience=AUDIENCE
    )


@pytest.fixture
async def client(
    base_url: str, token: str, request: pytest.FixtureRequest, phase_tracer: trace.Tracer
) -> AsyncIterator[Client]:
    async with Client(base_url=base_url, token=token) as opened:
        try:
            yield opened
        finally:
            if request.node.get_closest_marker("vm_sandboxes") is not None:
                for uid in opened.opened_sandbox_uids:
                    with phase_tracer.start_as_current_span("vm.diagnostics", attributes={"sandbox.uid": str(uid)}):
                        await native_artifacts.capture(opened, sandbox_uid=uid)


@pytest.fixture(params=[runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> runner_pb2.Harness:
    """Every scenario runs on every harness: one runner protocol, so one test body covers both."""
    return cast(runner_pb2.Harness, request.param)


@pytest.fixture
async def offered_model(client: Client, harness: runner_pb2.Harness) -> str:
    """A model this deployment offers for this harness, asked of the app rather than hardcoded."""
    return (await models_offered_for(client, [harness]))[harness]


@pytest.fixture
async def model(offered_model: str) -> str:
    """`offered_model`, loaded first if Ollama serves it: a real model's turn does not wait out a cold load."""
    await warm_ollama(offered_model)
    return offered_model


@pytest.fixture
async def scripted(scripted_listener: Listener) -> AsyncIterator[scripted_upstream.ScriptedUpstream]:
    """The model behind this target's LLM ingress, played by the scenario. One per test, so a request
    a scenario leaves unanswered fails that scenario."""
    upstream = scripted_upstream.ScriptedUpstream()
    async with upstream.serving(listener=scripted_listener.socket):
        yield upstream


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


@pytest.fixture(scope="session")
def cluster() -> Cluster:
    """Explicit live deployment access; k3s suites override this with their own cluster."""
    return Cluster(
        kubeconfig=Path(os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config"))),
        namespace=os.environ.get(NAMESPACE, STAGING_NAMESPACE),
        kubectl_command=("kubectl",),
    )
