"""Values used to launch the real runner executable in integration tests."""

from dataclasses import dataclass
from pathlib import Path

from agentplane.sandboxes.launch import SandboxIdentity


@dataclass(frozen=True)
class SandboxLaunch:
    """Launch values used by runner integration tests."""

    sandbox: SandboxIdentity
    bootstrap: str | None
    platform_instructions: str


@dataclass(frozen=True)
class NativeLaunch:
    binary: Path
    base_url: str
    token: str


@dataclass(frozen=True)
class RunnerTestConfig:
    state_dir: Path
    launch: SandboxLaunch
    harness_environment: dict[str, str]
    claude: NativeLaunch | None = None
    codex: NativeLaunch | None = None
    debug_checkpoint: tuple[str, str] | None = None
    codex_executor_url: str | None = None
    codex_executor_socket: Path | None = None
