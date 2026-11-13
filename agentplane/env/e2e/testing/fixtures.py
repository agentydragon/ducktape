"""Reusable runner fixtures and test helpers.

Keep these in a normal module so other packages can register the fixtures without
importing this package's conftest; tests can import helper types here directly.
Only the model fixture knows the harness dialect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agentplane.env.config import Endpoint, SVIDEndpoint
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.driver import RunnerDriver
from agentplane.env.e2e.testing.failure_journal import FailureEvidence
from agentplane.env.e2e.testing.gateway import Gateway, serve_gateway
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService, go_history_service
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.testing.claude_model import ClaudeModel
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.scripted_model import ScriptedModel

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(params=[session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> session_pb2.Harness:
    return cast(session_pb2.Harness, request.param)


ModelEndpoint = AnthropicMessages | OpenAIResponses


@pytest.fixture
async def endpoint(harness: session_pb2.Harness) -> AsyncIterator[ModelEndpoint]:
    server: ModelEndpoint = AnthropicMessages() if harness == session_pb2.HARNESS_CLAUDE else OpenAIResponses()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def model(harness: session_pb2.Harness, endpoint: ModelEndpoint) -> ScriptedModel[Any]:
    if harness == session_pb2.HARNESS_CLAUDE:
        assert isinstance(endpoint, AnthropicMessages)
        return ClaudeModel(endpoint)
    if harness == session_pb2.HARNESS_CODEX:
        assert isinstance(endpoint, OpenAIResponses)
        return CodexModel(endpoint)
    raise ValueError(f"unsupported {harness=}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as started:
        yield started


@pytest.fixture
def history_writer_namespaces(sandbox: SandboxCluster) -> Sequence[str]:
    return (sandbox.namespace, WRITER.namespace)


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    history_writer_namespaces: Sequence[str],
    runner_gateway: Gateway,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=history_writer_namespaces,
        svid_ca=runner_gateway.ca,
        policy_namespace=runner_gateway.sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
def spec(harness: session_pb2.Harness, workspace: Path, sandbox: SandboxCluster) -> session_pb2.SessionSpec:
    return launches.spec(harness, workspace, sandbox.identity)


@pytest.fixture
def config(
    harness: session_pb2.Harness, endpoint: ModelEndpoint, sandbox: SandboxCluster, tmp_path: Path
) -> RunnerTestConfig:
    return launches.config(
        harness, endpoint.origin, sandbox=sandbox.identity, state_dir=tmp_path / "state", home=tmp_path / "home"
    )


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest, config: RunnerTestConfig, runner_gateway: Gateway, history_service: GoHistoryService
) -> AsyncIterator[RunningRunner]:
    """The production Go runner, pushing its journal to the Go History Service ingress."""
    launched = replace(config, launch=replace(config.launch, sandbox=runner_gateway.sandbox.identity))
    async with history_tunnel(history_service.ingress_target) as proxy:
        ingress = Endpoint(
            target=history_service.ingress_target,
            svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address=proxy),
        )
        async with RunningRunner(
            launched, runner_gateway, history=history_service.reader, ingress_endpoint=ingress
        ) as started:
            yield started
            if (evidence := FailureEvidence.for_failed_call(request)) is not None:
                await evidence.preserve_runner(started)


@pytest.fixture
async def runner_gateway(sandbox: SandboxCluster, tmp_path: Path) -> AsyncIterator[Gateway]:
    async with serve_gateway(sandbox, tmp_path) as gateway:
        yield gateway


@pytest.fixture
def gateway(runner_gateway: Gateway) -> Gateway:
    return runner_gateway


@pytest.fixture
async def driver(runner: RunningRunner, history_service: GoHistoryService) -> AsyncIterator[RunnerDriver]:
    """Drive the running Go executable through its configured control listener."""
    async with RunnerDriver(runner.gateway, history_service.reader) as started:
        yield started
