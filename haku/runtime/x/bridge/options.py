"""Claude Code as a bridge backend: the launch the console asks for, and the binary that answers.

Both halves of what `backend.CliBackend` calls a backend, kept in one module because they have
to agree: the flags below only mean anything to the executable `ClaudeBackend` starts. Everything
generic about running *an* agent CLI is <backend.py>.

Replaces `ClaudeAgentOptions` plus `SubprocessCLITransport._build_command()`. That pairing was
the last of the Agent SDK the console used, and it was reached through a private method on a
transport we never let connect — we constructed one purely to borrow its argv builder, because a
custom `Transport` never sees the arguments it would have assembled.

**Why owning it is smaller than borrowing it.** `_build_command` translates ~40 options; the
console sets seven. Everything else was branches we never took, on a private API, pinned to an
exact SDK version, reached by assigning `transport._cli_path` from outside. What replaces it is
one function over a frozen dataclass of the seven, and `test_options.py` pins the exact argv —
so a change to what we launch is a visible diff rather than a consequence of someone else's
refactor.

The flag spellings and their order match what the SDK emitted for these options at 0.2.128,
verified against its source, so this is not a behavioural change to the launch — only to who
computes it. The CLI's own protocol reference is <../../../cli_protocol/README.md>.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from haku.runtime.x.bridge.backend import ProcessLaunch, child_environment
from haku.runtime.x.bridge.protocol import FINE_GRAINED_TOOL_STREAMING_ENV, ClaudeLaunch

# What the CLI reports itself as. The SDK sent `sdk-py`; the console is not the SDK, and the CLI
# treats this as a label rather than switching behaviour on it.
ENTRYPOINT = "haku-console"

# Where the sandbox image put the Claude executable. Named per-backend rather than by one shared
# `HAKU_CLI_PATH` on purpose: a second CLI ships in its own image with its own path, so it can
# name its own variable, and this one keeps the name the runner image and the bridge end-to-end
# test already set — a rename here would have to be a flag day for no gain.
EXECUTABLE_VARIABLE = "HAKU_CLAUDE_PATH"


@dataclass(frozen=True, slots=True)
class HttpMcpServer:
    """A streamable-HTTP MCP server the CLI should connect to."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def as_config(self) -> dict[str, object]:
        return {"type": "http", "url": self.url, "headers": dict(self.headers)}


@dataclass(frozen=True, slots=True)
class ClaudeSession:
    """Everything the console chooses about the CLI process behind one session.

    Deliberately not a general options object. A field here is one the console actually sets;
    anything else is a decision we have not made, and adding a field is the moment to make it.
    """

    append_system_prompt: str | None = None
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: Mapping[str, HttpMcpServer] = field(default_factory=dict)
    # No settings files, user or project: the session's behaviour comes from what the console
    # sends, so a file on the sandbox image cannot quietly change it.
    setting_sources: tuple[str, ...] = ()
    permission_mode: str = "bypassPermissions"


def build_claude_launch(session: ClaudeSession) -> ClaudeLaunch:
    """The argv, cwd and environment for one CLI process."""
    arguments = ["--output-format", "stream-json", "--verbose"]
    if session.append_system_prompt is not None:
        arguments += ["--append-system-prompt", session.append_system_prompt]
    arguments += ["--permission-mode", session.permission_mode]
    if session.mcp_servers:
        servers = {name: server.as_config() for name, server in session.mcp_servers.items()}
        arguments += ["--mcp-config", json.dumps({"mcpServers": servers})]
    # Streams text and incremental tool-input JSON. The env switch is the other half of the
    # same request and is set here rather than by a caller, so the two cannot drift apart.
    arguments += ["--include-partial-messages"]
    # Only the servers above: without this the CLI would union them with whatever is configured
    # on the sandbox image.
    arguments += ["--strict-mcp-config"]
    arguments += [f"--setting-sources={','.join(session.setting_sources)}"]
    # Last, and always: the CLI reads prompts and control requests as newline-delimited JSON on
    # stdin. Everything the console does depends on it.
    arguments += ["--input-format", "stream-json"]
    return ClaudeLaunch(
        arguments=tuple(arguments),
        cwd=str(session.cwd) if session.cwd is not None else ".",
        environment={"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT, FINE_GRAINED_TOOL_STREAMING_ENV: "1", **session.environment},
    )


@dataclass(frozen=True, slots=True)
class ClaudeBackend:
    """Claude Code, as the sandbox runner starts it and reads it back."""

    name: ClassVar[str] = "claude"
    # No agent-assigned identity, so a console cannot recognise a second copy of one, and its
    # reconstruction is `streamed += delta` — a replay double-appends. Also the class that never
    # needs replaying: whatever it built is superseded by the completed `assistant` frame behind
    # it, which does carry an id (<../../../cli_protocol/frame_identity.py>).
    DELTA_TYPE: ClassVar[str] = "stream_event"

    executable: Path

    def resolve(self, launch: ClaudeLaunch) -> ProcessLaunch:
        return ProcessLaunch(
            executable=self.executable,
            arguments=launch.arguments,
            cwd=launch.cwd,
            environment=child_environment(launch),
        )

    def replayable(self, payload: dict[str, Any]) -> bool:
        return payload.get("type") != self.DELTA_TYPE


def claude_backend(executable: Path | None = None) -> ClaudeBackend:
    """Claude Code at the path the sandbox image chose, or at *executable* when one is given."""
    return ClaudeBackend(
        executable=executable if executable is not None else Path(os.environ.get(EXECUTABLE_VARIABLE, "claude"))
    )
