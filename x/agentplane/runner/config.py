"""Runner-owned launch configuration. Credentials and endpoints never cross the protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


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
    # Holds `sessions/<session_id>/` with the session log and the harness's own persistence.
    state_dir: Path
    # Base environment of every harness child, as --harness-env gave it; native credentials are
    # added per launch.
    environment: Mapping[str, str] = field(default_factory=dict)
    claude: ClaudeLaunch | None = None
    codex: CodexLaunch | None = None
    # A hidden test-only process-integration seam. Deployments never set it. Once the runner
    # reaches this boundary it appends DebugCheckpoint and waits to be killed by the test; a
    # replacement runner sees that persisted event and proceeds normally.
    test_debug_checkpoint: DebugCheckpoint | None = None
