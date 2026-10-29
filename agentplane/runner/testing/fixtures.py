"""Reusable runner fixtures and test helpers.

Keep these in a normal module so other packages can register the fixtures without
importing this package's conftest; tests can import helper types here directly.
Only the model fixture knows the harness dialect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.runner.configuration import Endpoint
from agentplane.runner.testing import launches
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.failure_journal import FailureEvidence
from agentplane.runner.testing.gateway import Gateway, serve_gateway
from agentplane.runner.testing.process import RunningRunner
from agentplane.testing.claude_model import ClaudeModel
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ScriptedModel

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(params=[runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> runner_pb2.Harness:
    return cast(runner_pb2.Harness, request.param)


ModelEndpoint = AnthropicMessages | OpenAIResponses


@pytest.fixture
async def endpoint(harness: runner_pb2.Harness) -> AsyncIterator[ModelEndpoint]:
    server: ModelEndpoint = AnthropicMessages() if harness == runner_pb2.HARNESS_CLAUDE else OpenAIResponses()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def model(harness: runner_pb2.Harness, endpoint: ModelEndpoint) -> ScriptedModel[Any]:
    if harness == runner_pb2.HARNESS_CLAUDE:
        assert isinstance(endpoint, AnthropicMessages)
        return ClaudeModel(endpoint)
    if harness == runner_pb2.HARNESS_CODEX:
        assert isinstance(endpoint, OpenAIResponses)
        return CodexModel(endpoint)
    raise ValueError(f"unsupported {harness=}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def spec(harness: runner_pb2.Harness, workspace: Path) -> runner_pb2.SessionSpec:
    return launches.spec(harness, workspace)


@pytest.fixture
def config(harness: runner_pb2.Harness, endpoint: ModelEndpoint, tmp_path: Path) -> RunnerTestConfig:
    return launches.config(harness, endpoint.origin, state_dir=tmp_path / "state", home=tmp_path / "home")


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest, config: RunnerTestConfig, runner_gateway: Gateway, history_service: GoHistoryService
) -> AsyncIterator[RunningRunner]:
    """The production Go runner, pushing its journal to the Go History Service ingress."""
    ingress = Endpoint(target=history_service.ingress_target, bearer=await history_service.writer_token(WRITER))
    async with RunningRunner(
        config, runner_gateway, history=history_service.reader, ingress_endpoint=ingress
    ) as started:
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)


@pytest.fixture
async def runner_gateway() -> AsyncIterator[Gateway]:
    async with serve_gateway() as gateway:
        yield gateway


@pytest.fixture
def gateway(runner_gateway: Gateway) -> Gateway:
    return runner_gateway


@pytest.fixture
async def driver(runner: RunningRunner, history_service: GoHistoryService) -> AsyncIterator[RunnerDriver]:
    """Drive the running Go executable through its configured control listener."""
    async with RunnerDriver(runner.gateway, history_service.reader) as started:
        yield started
