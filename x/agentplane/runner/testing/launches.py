"""Runner configuration for the pinned binaries under Bazel, wired to a scripted upstream."""

from __future__ import annotations

import os
from pathlib import Path

from util.bazel.runfiles import get_required_path
from x.agentplane.harness_tests.claude import harness as claude_harness
from x.agentplane.harness_tests.codex import harness as codex_harness
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

PROVIDERS = {"claude": pb.PROVIDER_CLAUDE, "codex": pb.PROVIDER_CODEX}
CLAUDE_BINARY = "claude_code_cli_linux_x64/claude"
CODEX_BINARY = "agentplane_codex_cli_linux_x64/bin/codex"
RUNNER_BINARY = "_main/x/agentplane/runner/main_bin"
TOKEN = "test-key"


def spec(provider: str, cwd: Path) -> pb.SessionSpec:
    return pb.SessionSpec(
        provider=PROVIDERS[provider],
        cwd=str(cwd),
        model={"claude": claude_harness.MODEL, "codex": codex_harness.MODEL}[provider],
        reasoning_effort=codex_harness.EFFORT,
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


def config(provider: str, upstream: ScriptedUpstream, *, state_dir: Path, home: Path) -> RunnerConfig:
    return RunnerConfig(
        state_dir=state_dir,
        environment=environment(home),
        claude=claude_launch(upstream) if provider == "claude" else None,
        codex=codex_launch(upstream) if provider == "codex" else None,
    )


def claude_launch(upstream: ScriptedUpstream) -> ClaudeLaunch:
    return ClaudeLaunch(
        binary=get_required_path(CLAUDE_BINARY),
        base_url=upstream.origin,
        auth_token=TOKEN,
        command_prefix=(claude_harness.dynamic_loader(),),
    )


def codex_launch(upstream: ScriptedUpstream) -> CodexLaunch:
    return CodexLaunch(binary=get_required_path(CODEX_BINARY), base_url=f"{upstream.origin}/v1", api_key=TOKEN)


def runner_command(provider: str, upstream: ScriptedUpstream, *, state_dir: Path) -> list[str]:
    """The runner as its own process, configured like `config` is."""
    command = [str(get_required_path(RUNNER_BINARY)), "--state-dir", str(state_dir)]
    if provider == "claude":
        launch = claude_launch(upstream)
        command += ["--claude-binary", str(launch.binary), "--anthropic-base-url", launch.base_url]
        for prefix in launch.command_prefix:
            command += ["--claude-command-prefix", prefix]
    else:
        codex = codex_launch(upstream)
        command += ["--codex-binary", str(codex.binary), "--openai-base-url", codex.base_url]
    return command
