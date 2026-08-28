"""Which agent CLI the sandbox runs, and how one process of it is assembled.

Almost nothing below the envelope is Claude-specific: the console sends argv, a working directory
and an environment, and the runner pumps the process's newline-delimited JSON across console rolls.
What is harness-specific sits at opposite ends of the wire — the provider's Console adapter chooses
its native launch and protocol, while a backend resolves the matching binary inside the sandbox.
Claude and Codex each name those once, so adding either remains an implementation rather than a
branch inside the runner; see <docs/second_backend.md>.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from haku.runtime.x.bridge.claude_projection import Projected
from haku.runtime.x.bridge.protocol import HarnessLaunch

# The exact-session credential used by the runner bridge and by the Agent at Console MCP. The
# runner keeps the claim-owned value out of launch overlays, and the console's deploy config
# refuses the name as a provider API-key variable.
BRIDGE_CREDENTIAL_VARIABLE = "HAKU_AGENT_SDK_RUNNER_TOKEN"


@dataclass(frozen=True, slots=True)
class ProcessLaunch:
    """One CLI process, fully decided: which binary, which argv, where, and in what environment.

    The console's `HarnessLaunch` carries every part of this except the binary, because the
    binary is the one part the console cannot know — it is a path inside a sandbox image whose
    tag the SandboxTemplate chose. Resolving the two into this is the backend's whole job.
    """

    executable: Path
    arguments: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]

    @property
    def command(self) -> list[str]:
        return [str(self.executable), *self.arguments]


def child_environment(launch: HarnessLaunch) -> dict[str, str]:
    """Overlay launch values while retaining the claim-owned exact-session credential."""
    return {
        **os.environ,
        **{key: value for key, value in launch.environment.items() if key != BRIDGE_CREDENTIAL_VARIABLE},
    }


class HarnessDriver(Protocol):
    """One CLI process's native-protocol companion under the neutral-operation generation.

    The runner owns the native protocol from the cut on (#4667): what a dispatched prompt is
    written as, how the handshake and interrupt are said, what an inbound control request is
    answered with, and — through `observe`/`admit` — what the stream means as neutral operations.
    One driver per CLI process, stateful, dead with it.
    """

    def initialize(self) -> dict[str, Any] | None:
        """The native frame to write before anything else, or None for a harness without one."""
        ...

    def compose_prompt(self, text: str) -> dict[str, Any]: ...

    def compose_interrupt(self) -> dict[str, Any] | None: ...

    def answer_control_request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The reply to write back for a CLI-initiated request, or None for any other frame."""
        ...

    def observe(self, frame_seq: int, payload: dict[str, Any]) -> Projected: ...

    def admit(self, prompt_id: UUID, *, after_batch_seq: int | None, frame_seq: int | None) -> Projected: ...


class CliBackend(Protocol):
    """One agent CLI this bridge knows how to run."""

    @property
    def name(self) -> str:
        """How this CLI is named to an operator: `--harness`, and the exit-status error."""

    def resolve(self, launch: HarnessLaunch) -> ProcessLaunch:
        """The process to start for *launch*."""
        ...

    def driver(self) -> HarnessDriver:
        """A fresh native-protocol driver for one CLI process.

        Raises `NotImplementedError` for a harness not yet ported to the neutral-operation
        generation, which fails the runner at start with the reason in the pod log — the sandbox
        never launches a CLI whose stream nothing can interpret.
        """
        ...
