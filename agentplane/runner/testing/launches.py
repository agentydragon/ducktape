"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from agentplane.native.claude.testing import model as claude_model
from agentplane.native.codex.testing import model as codex_model
from agentplane.proto import runner_pb2
from agentplane.runner.configuration import (
    Configuration,
    ContainerExecutor,
    DebugCheckpoint,
    Endpoint,
    EnvironmentVariables,
    Harness,
    PodEnvironment,
    ReverseControl,
    RunnerControl,
)
from agentplane.runner.testing.config import NativeLaunch, RunnerTestConfig, SandboxLaunch
from agentplane.sandboxes.launch import SandboxIdentity
from util.bazel.runfiles import get_required_path

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNNER_BINARY = "_main/agentplane/runner/cmd/agentplane-runner_/agentplane-runner"
TOKEN = "test-key"
PLATFORM_INSTRUCTIONS = "Test platform guidance."
SANDBOX = SandboxIdentity(namespace="test-sandboxes", name="test-sandbox", uid="test-sandbox-uid")
SANDBOX_LAUNCH = SandboxLaunch(sandbox=SANDBOX, bootstrap=None, platform_instructions=PLATFORM_INSTRUCTIONS)


def spec(harness: runner_pb2.Harness, cwd: Path) -> runner_pb2.SessionSpec:
    if harness == runner_pb2.HARNESS_CLAUDE:
        model = claude_model.MODEL
    elif harness == runner_pb2.HARNESS_CODEX:
        model = codex_model.MODEL
    else:
        raise ValueError(f"unsupported {harness=}")
    return runner_pb2.SessionSpec(harness=harness, cwd=str(cwd), model=model, reasoning_effort=codex_model.EFFORT)


def environment(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "NO_PROXY": "127.0.0.1,localhost",
        # Native tool subprocesses inherit this deliberately minimal env; standard utilities stay
        # available under hermetic RBE execution.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def config(
    harness: runner_pb2.Harness,
    endpoint: str,
    *,
    state_dir: Path,
    home: Path,
    codex_executor_url: str | None = None,
    codex_executor_socket: Path | None = None,
) -> RunnerTestConfig:
    return RunnerTestConfig(
        state_dir=state_dir,
        launch=SANDBOX_LAUNCH,
        harness_environment=environment(home),
        claude=claude_launch(endpoint) if harness == runner_pb2.HARNESS_CLAUDE else None,
        codex=codex_launch(endpoint) if harness == runner_pb2.HARNESS_CODEX else None,
        codex_executor_url=codex_executor_url,
        codex_executor_socket=codex_executor_socket,
    )


def claude_launch(endpoint: str) -> NativeLaunch:
    return NativeLaunch(binary=get_required_path(CLAUDE_BINARY), base_url=endpoint, token=TOKEN)


def codex_launch(endpoint: str) -> NativeLaunch:
    return NativeLaunch(binary=get_required_path(CODEX_BINARY), base_url=f"{endpoint}/v1", token=TOKEN)


def runner_command(
    harness: runner_pb2.Harness,
    endpoint: str,
    *,
    state_dir: Path,
    gateway: ReverseControl,
    ingress: Endpoint,
    test_debug_checkpoint: tuple[str, str] | None = None,
    bootstrap: str | None = None,
    codex_binary: Path | None = None,
    harness_env: Sequence[str] = (),
    runner_binary: Path | None = None,
) -> list[str]:
    """The runner as its own process with a scripted control peer and real History ingress.

    The Sandbox's files go beside `state_dir`.
    """
    if harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX):
        raise ValueError(f"unsupported {harness=}")
    launch_file = state_dir.parent / "launch.json"
    launch_file.write_text(json.dumps({"sandbox": SANDBOX.model_dump(), "bootstrap": bootstrap}))
    inherited: list[str] = ["HOME", "PATH", "NO_PROXY"]
    values: dict[str, str] = {}
    for entry in harness_env:
        name, separator, value = entry.partition("=")
        if separator:
            values[name] = value
        else:
            inherited.append(name)
    claude = claude_launch(endpoint) if harness == runner_pb2.HARNESS_CLAUDE else None
    codex = codex_launch(endpoint) if harness == runner_pb2.HARNESS_CODEX else None
    config = Configuration(
        version=2,
        environment=PodEnvironment(supervision="subprocess"),
        state_dir=str(state_dir),
        initialization_cwd=str(state_dir),
        control=gateway,
        ingress=ingress,
        platform_instructions=PLATFORM_INSTRUCTIONS,
        harness_environment=EnvironmentVariables(inherit=inherited, values=values),
        claude=Harness(binary=str(claude.binary), base_url=claude.base_url, token_env="ANTHROPIC_AUTH_TOKEN")
        if claude
        else None,
        codex=Harness(binary=str(codex_binary or codex.binary), base_url=codex.base_url, token_env="OPENAI_API_KEY")
        if codex
        else None,
        debug_checkpoint=DebugCheckpoint(name=test_debug_checkpoint[0], command_id=test_debug_checkpoint[1])
        if test_debug_checkpoint
        else None,
    )
    path = state_dir.parent / "runner.json"
    path.write_text(config.model_dump_json(exclude_none=True))
    return [
        str(runner_binary or get_required_path(RUNNER_BINARY)),
        "--config",
        str(path),
        "--launch-file",
        str(launch_file),
    ]


def command(config: RunnerTestConfig, *, control: RunnerControl, ingress: Endpoint) -> list[str]:
    """Serialize test launch values into the production executable's CLI."""
    config.state_dir.parent.mkdir(parents=True, exist_ok=True)
    launch_file = config.state_dir.parent / f"{config.state_dir.name}-launch.json"
    launch_file.write_text(
        json.dumps({"sandbox": config.launch.sandbox.model_dump(), "bootstrap": config.launch.bootstrap})
    )
    file = Configuration(
        version=2,
        state_dir=str(config.state_dir),
        initialization_cwd=str(config.state_dir),
        control=control,
        ingress=ingress,
        platform_instructions=config.launch.platform_instructions,
        harness_environment=EnvironmentVariables(values=config.harness_environment),
        claude=Harness(
            binary=str(config.claude.binary), base_url=config.claude.base_url, token_env="ANTHROPIC_AUTH_TOKEN"
        )
        if config.claude
        else None,
        codex=Harness(binary=str(config.codex.binary), base_url=config.codex.base_url, token_env="OPENAI_API_KEY")
        if config.codex
        else None,
        environment=PodEnvironment(
            supervision="subprocess",
            executor=ContainerExecutor(url=config.codex_executor_url, socket=str(config.codex_executor_socket))
            if config.codex_executor_url is not None
            else None,
        ),
        debug_checkpoint=DebugCheckpoint(name=config.debug_checkpoint[0], command_id=config.debug_checkpoint[1])
        if config.debug_checkpoint
        else None,
    )
    path = config.state_dir.parent / f"{config.state_dir.name}-runner.json"
    path.write_text(file.model_dump_json(exclude_none=True))
    return [str(get_required_path(RUNNER_BINARY)), "--config", str(path), "--launch-file", str(launch_file)]
