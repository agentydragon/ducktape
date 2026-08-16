"""The exact CLI launch the console asks for.

Pinned argv rather than spot-checked flags. This is the one place a session's behaviour is
chosen, it is invisible at runtime until something misbehaves in a sandbox, and it used to be
computed by a private SDK method — so the point of the test is that changing what we launch
shows up as a diff here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest_bazel

from haku.runtime.x.bridge.options import ENTRYPOINT, ClaudeSession, HttpMcpServer, build_claude_launch, claude_backend
from haku.runtime.x.bridge.protocol import FINE_GRAINED_TOOL_STREAMING_ENV

CONSOLE_SESSION = ClaudeSession(
    append_system_prompt="you are Haku",
    cwd=Path("/workspace"),
    environment={"HAKU_ROOM": "!room:example.org"},
    mcp_servers={"haku-console": HttpMcpServer(url="https://console/mcp", headers={"Authorization": "Bearer T"})},
)


def test_the_console_session_launch_is_exactly_this() -> None:
    launch = build_claude_launch(CONSOLE_SESSION)

    assert launch.arguments == (
        "--output-format",
        "stream-json",
        "--verbose",
        "--append-system-prompt",
        "you are Haku",
        "--permission-mode",
        "bypassPermissions",
        "--mcp-config",
        json.dumps(
            {
                "mcpServers": {
                    "haku-console": {
                        "type": "http",
                        "url": "https://console/mcp",
                        "headers": {"Authorization": "Bearer T"},
                    }
                }
            }
        ),
        "--include-partial-messages",
        "--strict-mcp-config",
        "--setting-sources=",
        "--input-format",
        "stream-json",
    )
    assert launch.cwd == "/workspace"


def test_streaming_is_requested_on_both_halves_at_once() -> None:
    """The flag and the env switch are one decision; a caller setting only one gets deltas
    without incremental tool input and no error saying so."""
    launch = build_claude_launch(CONSOLE_SESSION)

    assert "--include-partial-messages" in launch.arguments
    assert launch.environment[FINE_GRAINED_TOOL_STREAMING_ENV] == "1"


def test_the_caller_environment_wins_over_our_defaults() -> None:
    launch = build_claude_launch(ClaudeSession(environment={"CLAUDE_CODE_ENTRYPOINT": "probe"}))

    assert launch.environment["CLAUDE_CODE_ENTRYPOINT"] == "probe"


def test_a_session_that_chooses_nothing_still_gets_the_protocol() -> None:
    """An empty session is not a broken one: the stdin protocol and the empty setting sources
    are the invariants, not options."""
    launch = build_claude_launch(ClaudeSession())

    assert launch.arguments == (
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--include-partial-messages",
        "--strict-mcp-config",
        "--setting-sources=",
        "--input-format",
        "stream-json",
    )
    assert launch.cwd == "."
    assert launch.environment["CLAUDE_CODE_ENTRYPOINT"] == ENTRYPOINT


def test_no_mcp_config_is_sent_when_there_are_no_servers() -> None:
    """`--mcp-config {"mcpServers": {}}` and no flag at all are different to the CLI, and the
    sandbox image may carry servers of its own that `--strict-mcp-config` then excludes."""
    assert "--mcp-config" not in build_claude_launch(ClaudeSession()).arguments


def test_only_a_delta_is_withheld_from_a_console_that_adopts_this_session() -> None:
    """The runner asks the backend which frames survive being sent twice, and for Claude the
    answer is "all but `stream_event`" — the one kind with no id to recognise a duplicate by and
    the one the console reconstructs by appending."""
    backend = claude_backend(Path("/usr/local/bin/claude"))

    assert not backend.replayable({"type": "stream_event", "event": {}})
    assert backend.replayable({"type": "assistant", "message": {"id": "msg_01abc"}})


if __name__ == "__main__":
    pytest_bazel.main()
