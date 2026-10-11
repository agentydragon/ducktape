"""Reusable runner fixtures and test helpers.

Keep these in a normal module so other packages can register the fixtures without
importing this package's conftest; tests can import helper types here directly.
Only the model fixture knows the harness dialect.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import Runner, serve
from agentplane.runner.testing import launches
from agentplane.runner.testing.claude_model import ClaudeModel
from agentplane.runner.testing.codex_model import CodexModel
from agentplane.runner.testing.failure_journal import preserve_native_evidence
from agentplane.runner.testing.scripted_model import ScriptedModel

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


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


@dataclass
class RunnerHandle:
    runner: Runner
    port: int

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"


class RunnerClientFactory(Protocol):
    def __call__(self, target: str, *, capture_history: bool = False) -> RunnerClient: ...


@pytest.fixture
def runner_client_factory() -> RunnerClientFactory:
    def create(target: str, *, capture_history: bool = False) -> RunnerClient:
        return RunnerClient(target, channel_options=None, capture_history=capture_history)

    return create


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest, config: RunnerConfig, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[RunnerHandle]:
    """A served runner whose native evidence a failed test keeps in its undeclared outputs."""
    caplog.set_level(logging.INFO, logger="httpx")
    async with serve(config, address="127.0.0.1:0", grace_s=0) as (started, port):
        yield RunnerHandle(runner=started, port=port)
        await preserve_native_evidence(request, started)


@pytest.fixture
async def client(runner: RunnerHandle, runner_client_factory: RunnerClientFactory) -> AsyncIterator[RunnerClient]:
    async with runner_client_factory(runner.target, capture_history=True) as client:
        yield client
