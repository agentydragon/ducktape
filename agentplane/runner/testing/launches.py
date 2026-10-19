"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from agentplane.native.claude.testing import model as claude_model
from agentplane.native.codex.testing import model as codex_model
from agentplane.proto import runner_pb2
from agentplane.runner.testing.config import NativeLaunch, RunnerTestConfig, SandboxLaunch
from agentplane.sandboxes.guest_config import Endpoint
from agentplane.sandboxes.launch import SandboxIdentity
from util.bazel.runfiles import get_required_path

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNNER_BINARY = "_main/agentplane/runner/go/cmd/agentplane-runner_/agentplane-runner"
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


def config(harness: runner_pb2.Harness, endpoint: str, *, state_dir: Path, home: Path) -> RunnerTestConfig:
    return RunnerTestConfig(
        state_dir=state_dir,
        launch=SANDBOX_LAUNCH,
        harness_environment=environment(home),
        claude=claude_launch(endpoint) if harness == runner_pb2.HARNESS_CLAUDE else None,
        codex=codex_launch(endpoint) if harness == runner_pb2.HARNESS_CODEX else None,
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
    gateway: Endpoint,
    test_debug_checkpoint: tuple[str, str] | None = None,
    bootstrap: str | None = None,
    codex_binary: Path | None = None,
    harness_env: Sequence[str] = (),
    runner_binary: Path | None = None,
) -> list[str]:
    """The runner as its own process, configured like `config` is, dialing `gateway` for both its
    control stream and its ingress. The Sandbox's files go beside `state_dir`."""
    launch_file = state_dir.parent / "launch.json"
    launch_file.write_text(json.dumps({"sandbox": SANDBOX.model_dump(), "bootstrap": bootstrap}))
    platform_instructions_file = state_dir.parent / "platform-instructions.md"
    platform_instructions_file.write_text(PLATFORM_INSTRUCTIONS)
    command = [
        str(runner_binary or get_required_path(RUNNER_BINARY)),
        *("--state-dir", str(state_dir)),
        *("--launch-file", str(launch_file), "--platform-instructions-file", str(platform_instructions_file)),
        *("--control-target", gateway.target, "--control-bearer", gateway.bearer),
        *("--ingress-target", gateway.target, "--ingress-bearer", gateway.bearer),
    ]
    if harness == runner_pb2.HARNESS_CLAUDE:
        launch = claude_launch(endpoint)
        command += ["--claude-binary", str(launch.binary), "--anthropic-base-url", launch.base_url]
    elif harness == runner_pb2.HARNESS_CODEX:
        codex = codex_launch(endpoint)
        command += ["--codex-binary", str(codex_binary or codex.binary), "--openai-base-url", codex.base_url]
    else:
        raise ValueError(f"unsupported {harness=}")
    for entry in ("HOME", "PATH", "NO_PROXY", *harness_env):
        command += ["--harness-env", entry]
    if test_debug_checkpoint is not None:
        name, command_id = test_debug_checkpoint
        command += ["--test-debug-checkpoint-name", name, "--test-debug-checkpoint-command-id", command_id]
    return command


def command(config: RunnerTestConfig, *, control: Endpoint, ingress: Endpoint) -> list[str]:
    """Serialize test launch values into the production executable's CLI."""
    config.state_dir.parent.mkdir(parents=True, exist_ok=True)
    launch_file = config.state_dir.parent / f"{config.state_dir.name}-launch.json"
    launch_file.write_text(
        json.dumps({"sandbox": config.launch.sandbox.model_dump(), "bootstrap": config.launch.bootstrap})
    )
    instructions = config.state_dir.parent / f"{config.state_dir.name}-instructions.md"
    instructions.write_text(config.launch.platform_instructions)
    result = [
        str(get_required_path(RUNNER_BINARY)),
        "--state-dir",
        str(config.state_dir),
        "--launch-file",
        str(launch_file),
        "--platform-instructions-file",
        str(instructions),
        "--control-target",
        control.target,
        "--control-bearer",
        control.bearer,
        "--ingress-target",
        ingress.target,
        "--ingress-bearer",
        ingress.bearer,
    ]
    if config.claude is not None:
        result += ["--claude-binary", str(config.claude.binary), "--anthropic-base-url", config.claude.base_url]
    if config.codex is not None:
        result += ["--codex-binary", str(config.codex.binary), "--openai-base-url", config.codex.base_url]
    for name, value in config.harness_environment.items():
        result += ["--harness-env", f"{name}={value}"]
    if config.debug_checkpoint is not None:
        name, command_id = config.debug_checkpoint
        result += ["--test-debug-checkpoint-name", name, "--test-debug-checkpoint-command-id", command_id]
    return result
