"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import os
from pathlib import Path

from util.bazel.runfiles import get_required_path
from x.agentplane.harness_tests.claude import harness as claude_harness
from x.agentplane.harness_tests.codex import harness as codex_harness
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNNER_BINARY = "_main/x/agentplane/runner/main_bin"
TOKEN = "test-key"


def spec(harness: pb.Harness, cwd: Path) -> pb.SessionSpec:
    if harness == pb.HARNESS_CLAUDE:
        model = claude_harness.MODEL
    elif harness == pb.HARNESS_CODEX:
        model = codex_harness.MODEL
    else:
        raise ValueError(f"unsupported {harness=}")
    return pb.SessionSpec(harness=harness, cwd=str(cwd), model=model, reasoning_effort=codex_harness.EFFORT)


def environment(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "NO_PROXY": "127.0.0.1,localhost",
        # Native tool subprocesses inherit this deliberately minimal env; standard utilities stay
        # available under hermetic RBE execution.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def config(harness: pb.Harness, endpoint: str, *, state_dir: Path, home: Path) -> RunnerConfig:
    return RunnerConfig(
        state_dir=state_dir,
        environment=environment(home),
        claude=claude_launch(endpoint) if harness == pb.HARNESS_CLAUDE else None,
        codex=codex_launch(endpoint) if harness == pb.HARNESS_CODEX else None,
    )


def claude_launch(endpoint: str) -> ClaudeLaunch:
    return ClaudeLaunch(binary=get_required_path(CLAUDE_BINARY), base_url=endpoint, auth_token=TOKEN)


def codex_launch(endpoint: str) -> CodexLaunch:
    return CodexLaunch(binary=get_required_path(CODEX_BINARY), base_url=f"{endpoint}/v1", api_key=TOKEN)


def runner_command(
    harness: pb.Harness, endpoint: str, *, state_dir: Path, test_debug_checkpoint: tuple[str, str] | None = None
) -> list[str]:
    """The runner as its own process, configured like `config` is."""
    command = [str(get_required_path(RUNNER_BINARY)), "--state-dir", str(state_dir)]
    if harness == pb.HARNESS_CLAUDE:
        launch = claude_launch(endpoint)
        command += ["--claude-binary", str(launch.binary), "--anthropic-base-url", launch.base_url]
    elif harness == pb.HARNESS_CODEX:
        codex = codex_launch(endpoint)
        command += ["--codex-binary", str(codex.binary), "--openai-base-url", codex.base_url]
    else:
        raise ValueError(f"unsupported {harness=}")
    if test_debug_checkpoint is not None:
        name, command_id = test_debug_checkpoint
        command += ["--test-debug-checkpoint-name", name, "--test-debug-checkpoint-command-id", command_id]
    return command
