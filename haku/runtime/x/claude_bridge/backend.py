"""Which agent CLI the sandbox runs, and how one process of it is assembled.

Almost nothing below the envelope is Claude-specific. The console sends argv, a working
directory and an environment; the runner starts a process, pumps its newline-delimited JSON,
and holds it across a console roll. What *is* specific to one CLI is a small pair of decisions
at opposite ends of the wire — which flags mean "stream JSON and take prompts on stdin"
(`options.build_claude_launch`, console side) and which binary in the sandbox image answers to
them (`options.ClaudeBackend`, runner side) — plus one fact the runner cannot avoid knowing:
which of a CLI's frames survive being sent twice.

A backend is those decisions named once, so a second CLI is a second implementation rather than
a branch inside the runner. What such an implementation would have to provide, and what this
seam deliberately does not cover, is <docs/second_backend.md>.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from haku.runtime.x.claude_bridge.protocol import ClaudeLaunch

# The credential the runner dials the console with. A property of the bridge rather than of any
# CLI, which is why stripping it is here and not in a backend: it is ours whichever child runs.
BRIDGE_CREDENTIAL_VARIABLE = "HAKU_AGENT_SDK_RUNNER_TOKEN"


@dataclass(frozen=True, slots=True)
class ProcessLaunch:
    """One CLI process, fully decided: which binary, which argv, where, and in what environment.

    The console's `ClaudeLaunch` carries every part of this except the binary, because the
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


def child_environment(launch: ClaudeLaunch) -> dict[str, str]:
    """This process's environment with the launch overlaid and the bridge credential removed."""
    return {
        key: value for key, value in {**os.environ, **launch.environment}.items() if key != BRIDGE_CREDENTIAL_VARIABLE
    }


class CliBackend(Protocol):
    """One agent CLI this bridge knows how to run."""

    @property
    def name(self) -> str:
        """How this CLI is named to an operator: `--backend`, and the exit-status error."""

    def resolve(self, launch: ClaudeLaunch) -> ProcessLaunch:
        """The process to start for *launch*."""
        ...

    def replayable(self, payload: dict[str, Any]) -> bool:
        """Whether a console adopting this session mid-turn may be handed this frame again.

        The runner's one piece of CLI vocabulary, and it cannot be delegated to the console:
        the runner decides what to retain at the moment it sends, long before any adopting
        console exists. A frame with no agent-assigned identity cannot be recognised as a
        duplicate, and one the console accumulates (`streamed += delta`) is corrupted by a
        second copy — so which frames those are is a fact about a CLI's protocol, and therefore
        a fact about a backend.
        """
        ...
