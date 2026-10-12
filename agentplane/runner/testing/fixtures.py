"""Reusable runner fixtures and test helpers.

Keep these in a normal module so other packages can register the fixtures without
importing this package's conftest; tests can import helper types here directly.
Only the model fixture knows the harness dialect.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.runner import Runner
from agentplane.runner.testing import launches
from agentplane.runner.testing.claude_model import ClaudeModel
from agentplane.runner.testing.codex_model import CodexModel
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.failure_journal import FailureEvidence
from agentplane.runner.testing.gateway import Gateway, dialing, serve_gateway
from agentplane.runner.testing.scripted_model import ScriptedModel

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
def config(harness: runner_pb2.Harness, endpoint: ModelEndpoint, tmp_path: Path) -> RunnerConfig:
    return launches.config(harness, endpoint.origin, state_dir=tmp_path / "state", home=tmp_path / "home")


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest, config: RunnerConfig, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[Runner]:
    """A started runner whose native evidence a failed test keeps in its undeclared outputs."""
    caplog.set_level(logging.INFO, logger="httpx")
    async with Runner(config) as started:
        await started.startup()
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async with serve_gateway() as gateway:
        yield gateway


@pytest.fixture
async def driver(runner: Runner, gateway: Gateway) -> AsyncIterator[RunnerDriver]:
    """Drives `runner`, which dials `gateway` for the duration of the test."""
    async with dialing(runner, gateway):
        yield RunnerDriver(gateway)
