"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from python.runfiles import Runfiles

from agentplane.env.config import (
    Codex,
    Configuration,
    ContainerExecutor,
    DebugCheckpoint,
    Endpoint,
    EnvironmentRuntimeControl,
    EnvironmentVariables,
    Harness,
    PodEnvironment,
)
from agentplane.env.e2e.testing.config import NativeLaunch, RunnerTestConfig, SandboxLaunch
from agentplane.native.claude.testing import model as claude_model
from agentplane.native.codex.testing import model as codex_model
from agentplane.proto import session_pb2
from agentplane.sandboxes.launch import SandboxIdentity

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNTIME_BINARY = "ducktape/agentplane/env/runtime/cmd/agentplane-environment-runtime_/agentplane-environment-runtime"
EXECUTION_BINARY = (
    "ducktape/agentplane/env/runtime/execution/cmd/agentplane-execution-service_/agentplane-execution-service"
)
TOKEN = "test-key"
PLATFORM_INSTRUCTIONS = "Test platform guidance."


def spec(harness: session_pb2.Harness, cwd: Path, sandbox: SandboxIdentity) -> session_pb2.SessionSpec:
    if harness == session_pb2.HARNESS_CLAUDE:
        model = claude_model.MODEL
    elif harness == session_pb2.HARNESS_CODEX:
        model = codex_model.MODEL
    else:
        raise ValueError(f"unsupported {harness=}")
    return session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=sandbox.name, uid=sandbox.uid), harness=harness
            )
        ),
        cwd=str(cwd),
        model=model,
        reasoning_effort=codex_model.EFFORT,
    )


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
    harness: session_pb2.Harness,
    endpoint: str,
    *,
    sandbox: SandboxIdentity,
    state_dir: Path,
    home: Path,
    codex_executor_url: str | None = None,
    codex_executor_socket: Path | None = None,
) -> RunnerTestConfig:
    return RunnerTestConfig(
        state_dir=state_dir,
        launch=SandboxLaunch(sandbox=sandbox, bootstrap=None, platform_instructions=PLATFORM_INSTRUCTIONS),
        harness_environment=environment(home),
        claude=claude_launch(endpoint) if harness == session_pb2.HARNESS_CLAUDE else None,
        codex=codex_launch(endpoint) if harness == session_pb2.HARNESS_CODEX else None,
        codex_executor_url=codex_executor_url,
        codex_executor_socket=codex_executor_socket,
    )


def claude_launch(endpoint: str) -> NativeLaunch:
    return NativeLaunch(binary=Runfiles.CreateOrRaise().root() / CLAUDE_BINARY, base_url=endpoint, token=TOKEN)


def codex_launch(endpoint: str) -> NativeLaunch:
    return NativeLaunch(binary=Runfiles.CreateOrRaise().root() / CODEX_BINARY, base_url=f"{endpoint}/v1", token=TOKEN)


def runner_command(
    harness: session_pb2.Harness,
    endpoint: str,
    *,
    sandbox: SandboxIdentity,
    state_dir: Path,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    model_config_target: str,
    test_debug_checkpoint: tuple[str, str] | None = None,
    bootstrap: str | None = None,
    codex_binary: Path | None = None,
    harness_env: Sequence[str] = (),
    runner_binary: Path | None = None,
) -> list[str]:
    """The runner as its own process with a scripted control peer and real History ingress.

    The Sandbox's files go beside `state_dir`.
    """
    if harness not in (session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX):
        raise ValueError(f"unsupported {harness=}")
    launch_file = state_dir.parent / "launch.json"
    launch_file.write_text(json.dumps({"sandbox": sandbox.model_dump(), "bootstrap": bootstrap}))
    inherited: list[str] = ["HOME", "PATH", "NO_PROXY"]
    values: dict[str, str] = {}
    for entry in harness_env:
        name, separator, value = entry.partition("=")
        if separator:
            values[name] = value
        else:
            inherited.append(name)
    claude = claude_launch(endpoint) if harness == session_pb2.HARNESS_CLAUDE else None
    codex = codex_launch(endpoint) if harness == session_pb2.HARNESS_CODEX else None
    config = Configuration(
        version=2,
        environment=PodEnvironment(supervision="pid1"),
        state_dir=str(state_dir),
        initialization_cwd=str(state_dir),
        control=control,
        ingress=ingress,
        model_config_target=model_config_target,
        platform_instructions=PLATFORM_INSTRUCTIONS,
        harness_environment=EnvironmentVariables(inherit=inherited, values=values),
        claude=Harness(binary=str(claude.binary), base_url=claude.base_url, token_env="ANTHROPIC_AUTH_TOKEN")
        if claude
        else None,
        codex=Codex(binary=str(codex_binary or codex.binary), base_url=codex.base_url, token_env="OPENAI_API_KEY")
        if codex
        else None,
        debug_checkpoint=DebugCheckpoint(name=test_debug_checkpoint[0], command_id=test_debug_checkpoint[1])
        if test_debug_checkpoint
        else None,
    )
    path = state_dir.parent / "environment-runtime.json"
    path.write_text(config.model_dump_json(exclude_none=True))
    return [
        str(runner_binary or (Runfiles.CreateOrRaise().root() / RUNTIME_BINARY)),
        "--config",
        str(path),
        "--launch-file",
        str(launch_file),
    ]


def command(
    config: RunnerTestConfig, *, control: EnvironmentRuntimeControl, ingress: Endpoint, model_config_target: str
) -> list[str]:
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
        model_config_target=model_config_target,
        platform_instructions=config.launch.platform_instructions,
        harness_environment=EnvironmentVariables(values={**config.harness_environment, "PATH": "/usr/bin:/bin"}),
        claude=Harness(
            binary=str(config.claude.binary), base_url=config.claude.base_url, token_env="ANTHROPIC_AUTH_TOKEN"
        )
        if config.claude
        else None,
        codex=Codex(binary=str(config.codex.binary), base_url=config.codex.base_url, token_env="OPENAI_API_KEY")
        if config.codex
        else None,
        environment=PodEnvironment(
            supervision="pid1",
            executor=ContainerExecutor(url=config.codex_executor_url, socket=str(config.codex_executor_socket))
            if config.codex_executor_url is not None
            else None,
        ),
        debug_checkpoint=DebugCheckpoint(name=config.debug_checkpoint[0], command_id=config.debug_checkpoint[1])
        if config.debug_checkpoint
        else None,
    )
    path = config.state_dir.parent / f"{config.state_dir.name}-environment-runtime.json"
    path.write_text(file.model_dump_json(exclude_none=True))
    return [
        str(Runfiles.CreateOrRaise().root() / RUNTIME_BINARY),
        "--config",
        str(path),
        "--launch-file",
        str(launch_file),
    ]
