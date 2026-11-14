"""Minimal pytest fixtures for App-free, native system-test scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from opentelemetry import trace

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients, native_platform_clients
from agentplane.system_tests.driver.sandbox import Sandboxes, sandbox_scope
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import timing_summary, tracer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener
from util.testing.otel_tracing import configure_tracing

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(autouse=True)
def phase_tracer(request: pytest.FixtureRequest) -> Iterator[trace.Tracer]:
    with tracer.start_as_current_span("system_test.case", attributes={"pytest.nodeid": request.node.nodeid}):
        yield tracer


@pytest.fixture
async def scripted(scripted_listener: Listener) -> AsyncIterator[ScriptedUpstream]:
    """Play one strict scripted model per case over the deployment's existing listener."""
    upstream = ScriptedUpstream()
    async with upstream.serving(listener=scripted_listener.socket):
        yield upstream


@pytest.fixture
async def native_platform(cluster: Cluster, tmp_path: Path) -> AsyncIterator[NativePlatformClients]:
    async with native_platform_clients(cluster, tmp_path / "native-platform") as clients:
        yield clients


@pytest.fixture
async def native_sandbox(
    cluster: Cluster, native_platform: NativePlatformClients, phase_tracer: trace.Tracer
) -> AsyncIterator[Sandboxes]:
    async with sandbox_scope(cluster, native_platform.environment, phase_tracer) as sandboxes:

        async def create_ready(
            name: str,
            *,
            template: str | None = None,
            policies: list[str] | None = None,
            preset: str | None = None,
            bootstrap: str = "",
            inbox_tags: Sequence[str] = (),
            idle_suspend_after_seconds: int | None = None,
        ) -> environment_service_pb2.EnvironmentDescriptor:
            view = await sandboxes(
                name,
                template=template,
                policies=policies,
                preset=preset,
                bootstrap=bootstrap,
                inbox_tags=inbox_tags,
                idle_suspend_after_seconds=idle_suspend_after_seconds,
            )
            await native_platform.ready_runner(view.target)
            return view

        yield Sandboxes(sandboxes.environment, sandboxes.opened_environment_uids, create_ready)


def pytest_collection_finish(session: pytest.Session) -> None:
    configure_tracing(filename="test-spans.jsonl")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "scripted_model: the scenario plays the model (`scripted` fixture)")
    config.addinivalue_line("markers", "vm_sandboxes: needs a deployment that runs VM Sandboxes")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> object:
    with tracer.start_as_current_span("system_test.scenario", attributes={"pytest.nodeid": item.nodeid}):
        return (yield)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_sep("-", "OpenTelemetry stage timings (inclusive; concurrent spans overlap)")
    terminalreporter.write_line(timing_summary(undeclared_outputs_dir() / "test-spans.jsonl"))
