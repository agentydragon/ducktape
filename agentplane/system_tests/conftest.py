"""Shared pytest tracing, harness parameters, and scripted-model fixtures.

K3s suites supply explicit deployment access. The separate live targets read their configuration
once at the fixture boundary and default to staging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator, Iterator
from typing import cast

import pytest
from opentelemetry import trace

from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import scripted_upstream
from agentplane.system_tests.driver.case_batch import CASE_RESULT
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import native_client
from agentplane.system_tests.driver.tracing import timing_summary, tracer
from agentplane.system_tests.k3s.pytest_plugin import k3s_binary
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.otel_tracing import configure_tracing
from util.net import Listener

# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "case_batch: selected cases own their async execution and trace spans")
    config.addinivalue_line("markers", "case_timeout(seconds): bound scenario execution after shared setup")
    config.addinivalue_line("markers", "exclusive_case: run alone before the other cases in this module")
    config.addinivalue_line("markers", "vm_sandboxes: needs a deployment that runs VM Sandboxes")
    config.addinivalue_line("markers", "real_model: its evidence is what a real model does")
    config.addinivalue_line("markers", "scripted_model: the scenario plays the model (`scripted` fixture)")


def pytest_collection_finish(session: pytest.Session) -> None:
    configure_tracing(filename="test-spans.jsonl")


@pytest.fixture(autouse=True)
def phase_tracer(request: pytest.FixtureRequest) -> Iterator[trace.Tracer]:
    if request.node.get_closest_marker("case_batch") is not None:
        yield tracer
        return
    with tracer.start_as_current_span("system_test.case", attributes={"pytest.nodeid": request.node.nodeid}):
        yield tracer


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> object:
    if item.get_closest_marker("case_batch") is not None:
        return (yield)
    with tracer.start_as_current_span("system_test.scenario", attributes={"pytest.nodeid": item.nodeid}):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if call.when == "call" and CASE_RESULT in item.stash:
        # Pytest waits for already-running tasks sequentially. Report the task's real
        # elapsed duration, rather than just the time spent waiting for its result.
        report.duration = item.stash[CASE_RESULT].seconds
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
async def scripted(scripted_listener: Listener) -> AsyncIterator[scripted_upstream.ScriptedUpstream]:
    """The model behind this target's LLM ingress, played by the scenario. One per test, so a request
    a scenario leaves unanswered fails that scenario."""
    upstream = scripted_upstream.ScriptedUpstream()
    async with upstream.serving(listener=scripted_listener.socket):
        yield upstream


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_sep("-", "OpenTelemetry stage timings (inclusive; concurrent spans overlap)")
    terminalreporter.write_line(timing_summary(undeclared_outputs_dir() / "test-spans.jsonl"))
