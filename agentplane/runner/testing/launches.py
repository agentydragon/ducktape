"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from agentplane.harness_tests.claude import model as claude_model
from agentplane.harness_tests.codex import model as codex_model
from agentplane.proto import runner_pb2
from agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig, ServiceEndpoint
from agentplane.runner.model_config import HttpModelConfigResolver
from agentplane.runner.sandbox_launch import SandboxIdentity, SandboxLaunch
from util.bazel.runfiles import get_required_path

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNNER_BINARY = "_main/agentplane/runner/main_bin"
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


def unconfigured_models() -> HttpModelConfigResolver:
    """An ingress that configures no model, so every session uses its harness's defaults."""
    return HttpModelConfigResolver(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"null")))


def config(harness: runner_pb2.Harness, endpoint: str, *, state_dir: Path, home: Path) -> RunnerConfig:
    return RunnerConfig(
        state_dir=state_dir,
        launch=SANDBOX_LAUNCH,
        harness_environment=environment(home),
        model_config_resolver=unconfigured_models(),
        claude=claude_launch(endpoint) if harness == runner_pb2.HARNESS_CLAUDE else None,
        codex=codex_launch(endpoint) if harness == runner_pb2.HARNESS_CODEX else None,
    )


def claude_launch(endpoint: str) -> ClaudeLaunch:
    return ClaudeLaunch(binary=get_required_path(CLAUDE_BINARY), base_url=endpoint, auth_token=TOKEN)


def codex_launch(endpoint: str) -> CodexLaunch:
    return CodexLaunch(binary=get_required_path(CODEX_BINARY), base_url=f"{endpoint}/v1", api_key=TOKEN)


def runner_command(
    harness: runner_pb2.Harness,
    endpoint: str,
    *,
    state_dir: Path,
    gateway: ServiceEndpoint,
    test_debug_checkpoint: tuple[str, str] | None = None,
    bootstrap: str | None = None,
) -> list[str]:
    """The runner as its own process, configured like `config` is, dialing `gateway` for both its
    control stream and its ingress. The Sandbox's files go beside `state_dir`."""
    launch_file = state_dir.parent / "launch.json"
    launch_file.write_text(json.dumps({"sandbox": SANDBOX.model_dump(), "bootstrap": bootstrap}))
    platform_instructions_file = state_dir.parent / "platform-instructions.md"
    platform_instructions_file.write_text(PLATFORM_INSTRUCTIONS)
    command = [
        str(get_required_path(RUNNER_BINARY)),
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
        command += ["--codex-binary", str(codex.binary), "--openai-base-url", codex.base_url]
    else:
        raise ValueError(f"unsupported {harness=}")
    if test_debug_checkpoint is not None:
        name, command_id = test_debug_checkpoint
        command += ["--test-debug-checkpoint-name", name, "--test-debug-checkpoint-command-id", command_id]
    return command
