"""Claude Code launch material: the argv, cwd and environment the console asks the runner to run.

The harness that starts and drives this process is <claude_harness.py> `ClaudeHarness`; this module
owns only what configures it. They live apart but agree: the flags below only mean anything to the
executable the harness starts.

`build_claude_launch` is the one place a session's argv is decided, and `test_claude_options.py` pins
it exactly. The flag spellings and their order match what the Agent SDK emitted for these options at
0.2.128. The CLI's own protocol reference is <../../../cli_protocol/README.md>.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from haku.runtime.x.bridge.protocol import FINE_GRAINED_TOOL_STREAMING_ENV, HarnessLaunch

# What the CLI reports itself as. A label: the CLI does not switch behaviour on it.
ENTRYPOINT = "haku-console"

# Where the sandbox image put the Claude executable. Named per-backend rather than by one shared
# `HAKU_CLI_PATH`, since a second CLI ships in its own image with its own path and can name its own
# variable.
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


def build_claude_launch(session: ClaudeSession, *, resume_from: int | None = None) -> HarnessLaunch:
    """The argv, cwd and environment for one CLI process, and where its console has got to.

    *resume_from* is not part of the process: it is the highest frame number this console has
    recorded for the session, riding on `start` because that frame is sent on every connection
    (`HarnessLaunch.resume_from`). None means nothing recorded yet, and the runner replays its whole
    window.
    """
    arguments = ["--output-format", "stream-json", "--verbose"]
    if session.append_system_prompt is not None:
        arguments += ["--append-system-prompt", session.append_system_prompt]
    arguments += ["--permission-mode", session.permission_mode]
    if session.mcp_servers:
        servers = {name: server.as_config() for name, server in session.mcp_servers.items()}
        arguments += ["--mcp-config", json.dumps({"mcpServers": servers})]
    # Streams text and incremental tool-input JSON. The env switch below is the other half of the
    # same request, set here so the two cannot drift apart.
    arguments += ["--include-partial-messages"]
    # Only the servers above: without this the CLI would union them with whatever is configured
    # on the sandbox image.
    arguments += ["--strict-mcp-config"]
    arguments += [f"--setting-sources={','.join(session.setting_sources)}"]
    # Last, and always: the CLI reads prompts and control requests as newline-delimited JSON on
    # stdin. Everything the console does depends on it.
    arguments += ["--input-format", "stream-json"]
    return HarnessLaunch(
        arguments=tuple(arguments),
        cwd=str(session.cwd) if session.cwd is not None else ".",
        environment={"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT, FINE_GRAINED_TOOL_STREAMING_ENV: "1", **session.environment},
        resume_from=resume_from,
    )
