"""Claude Agent SDK options shared by the remote conversation runtime."""

from __future__ import annotations

from dataclasses import replace

from claude_agent_sdk import ClaudeAgentOptions, __version__ as sdk_version
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from haku.runtime.x.agent_sdk_transport.protocol import FINE_GRAINED_TOOL_STREAMING_ENV, ClaudeLaunch


def enable_fine_grained_streaming(options: ClaudeAgentOptions) -> ClaudeAgentOptions:
    """Return options that stream text and incremental tool-input JSON.

    Set the CLI environment switch explicitly as part of Haku's runtime
    contract rather than depending on private coupling inside the pinned SDK.
    """

    return replace(options, include_partial_messages=True, env={**options.env, FINE_GRAINED_TOOL_STREAMING_ENV: "1"})


def build_claude_launch(options: ClaudeAgentOptions) -> ClaudeLaunch:
    """Translate SDK options into the CLI launch handshake for the sandbox.

    Agent SDK custom transports do not receive the CLI arguments normally built
    by ``SubprocessCLITransport``. Keep that version-sensitive translation in
    the trusted process and cover it with the repository's exact SDK pin.
    """
    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "claude"
    command = transport._build_command()
    cwd = str(options.cwd) if options.cwd is not None else "."
    environment = {"CLAUDE_CODE_ENTRYPOINT": "sdk-py", **options.env, "CLAUDE_AGENT_SDK_VERSION": sdk_version}
    return ClaudeLaunch(arguments=tuple(command[1:]), cwd=cwd, environment=environment)
