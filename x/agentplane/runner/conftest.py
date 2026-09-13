"""One interaction script runs against both harnesses; only the model fixture knows the dialect."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.process import serve as serve_upstream
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.config import RunnerConfig
from x.agentplane.runner.service import Runner, serve
from x.agentplane.runner.testing import launches
from x.agentplane.runner.testing.claude_model import ClaudeModel
from x.agentplane.runner.testing.codex_model import CodexModel
from x.agentplane.runner.testing.scripted_model import ScriptedModel

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@pytest.fixture(params=[pb.HARNESS_CLAUDE, pb.HARNESS_CODEX], ids=["claude", "codex"])
def harness(request: pytest.FixtureRequest) -> pb.Harness.ValueType:
    return pb.Harness.ValueType(request.param)


@pytest.fixture
def upstream() -> Iterator[ScriptedUpstream]:
    with serve_upstream(ScriptedUpstream()) as server:
        yield server


@pytest.fixture
def model(harness: pb.Harness.ValueType, upstream: ScriptedUpstream) -> ScriptedModel:
    if harness == pb.HARNESS_CLAUDE:
        return ClaudeModel(upstream)
    if harness == pb.HARNESS_CODEX:
        return CodexModel(upstream)
    raise ValueError(f"unsupported {harness=}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def spec(harness: pb.Harness.ValueType, workspace: Path) -> pb.SessionSpec:
    return launches.spec(harness, workspace)


@pytest.fixture
def config(harness: pb.Harness.ValueType, upstream: ScriptedUpstream, tmp_path: Path) -> RunnerConfig:
    return launches.config(harness, upstream, state_dir=tmp_path / "state", home=tmp_path / "home")


@dataclass
class RunnerHandle:
    server: grpc.aio.Server
    runner: Runner
    port: int

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def stop(self) -> None:
        await self.runner.stop()
        await self.server.stop(0)


@pytest.fixture
async def runner(config: RunnerConfig) -> AsyncIterator[RunnerHandle]:
    server, started, port = await serve(config)
    handle = RunnerHandle(server, started, port)
    yield handle
    await handle.stop()


@pytest.fixture
async def client(runner: RunnerHandle) -> AsyncIterator[RunnerClient]:
    client = RunnerClient(runner.target)
    yield client
    await client.close()
