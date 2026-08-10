# Agent SDK remote transport

Haku Console will own `ClaudeSDKClient` and its `Query` control layer while the bundled Claude CLI
runs inside a Haku sandbox. This package carries the SDK's existing stream-JSON protocol over a text
WebSocket instead of defining a second conversation protocol. The repository pins Agent SDK 0.2.128,
which bundles Claude Code 2.1.220 and types the required `dontAsk` permission mode directly.

`WebSocketTransport` is the Console-side custom Agent SDK transport. The sandbox runs the
`//haku/runtime/x/agent_sdk_transport:runner_bin` Python executable, which opens the WebSocket, starts a
configured local Claude Code executable, and copies newline-delimited JSON between the socket and
Claude's stdin/stdout. The runner imports no Agent SDK code. On connect, the trusted Console process
sends a versioned `haku_transport/start` frame containing the CLI arguments, working directory, and
explicit environment produced from its `ClaudeAgentOptions`; this preserves dynamic options such as
resume, system prompts, and SDK MCP configuration without moving SDK orchestration into the sandbox.
A compatibility test locates the real Claude executable bundled in the pinned SDK wheel and verifies
that the launch command matches `SubprocessCLITransport` exactly. Subsequent WebSocket frames are the
unmodified JSON objects exchanged by the SDK and CLI; the reserved `haku_transport/end_input` frame
represents the one transport operation that is not itself a JSON protocol message.

The runtime enables both partial messages and fine-grained tool streaming. Console therefore receives
incremental text and `input_json_delta` tool-argument events, followed by complete `ToolUseBlock` and
`ToolResultBlock` values for durable projections.

The runner accepts `--websocket-url` and `--claude-path`, and optionally sends the
`HAKU_AGENT_SDK_RUNNER_TOKEN` environment value as a bearer credential. That bridge credential is
removed from the child CLI environment. The sandbox image must make the pinned Claude executable
available at the configured path. This package
deliberately does not yet provision sandboxes, mount the transport in Haku Console, or expose tools.
Those are subsequent vertical slices once the executable transport boundary is covered independently.
