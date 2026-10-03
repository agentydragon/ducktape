"""Runner-owned launch configuration. Credentials and endpoints never cross the protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agentplane.runner.cgroups import AgentCgroups


@dataclass(frozen=True, slots=True)
class ClaudeLaunch:
    binary: Path
    # Anthropic Messages endpoint the harness talks to, without a path.
    base_url: str
    auth_token: str


@dataclass(frozen=True, slots=True)
class CodexLaunch:
    binary: Path
    # OpenAI Responses base URL including its `/v1`.
    base_url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class DebugCheckpoint:
    """One test-only runner pause, surfaced through the ordinary session event stream."""

    name: str
    command_id: str


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    # Holds runner-owned logs and metadata.
    state_dir: Path
    # Base environment of every harness child, as --harness-env gave it; native credentials are
    # added per launch.
    environment: Mapping[str, str] = field(default_factory=dict)
    # Per-route context windows for harness models with verified non-default limits. These apply to
    # each harness process selected for that session; model changes across different limits are
    # refused because neither harness can safely update its compaction window mid-thread.
    model_context_windows: Mapping[str, int] = field(default_factory=dict)
    claude: ClaudeLaunch | None = None
    codex: CodexLaunch | None = None
    # VM guests keep app work in a delegated cgroup and run it under a separate unprivileged UID.
    # Container deployments leave this unset and retain their existing process behavior.
    process_isolation: AgentCgroups | None = None
    # When set, new and retained session working directories must stay beneath this mount.
    workspace_root: Path | None = None
    # VM guests separate native harness histories from the runner's journal tree so project quota
    # can keep untrusted harness writes from consuming journal capacity. Containers keep the
    # existing per-session layout by leaving this unset.
    native_state_dir: Path | None = None
    # VM initialization/setup scripts work on the workspace disk, not the runner-owned state tree.
    initialization_cwd: Path | None = None
    # A hidden test-only process-integration seam. Deployments never set it. Once the runner
    # reaches this boundary it appends DebugCheckpoint and waits to be killed by the test; a
    # replacement runner sees that persisted event and proceeds normally.
    test_debug_checkpoint: DebugCheckpoint | None = None
