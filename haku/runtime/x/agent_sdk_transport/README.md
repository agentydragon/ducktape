# Agent SDK remote transport

Haku Console will own `ClaudeSDKClient` and its `Query` control layer while the bundled Claude CLI
runs inside a Haku sandbox. This package carries the SDK's existing stream-JSON protocol over a text
WebSocket instead of defining a second conversation protocol. The repository pins Agent SDK 0.2.128,
which bundles Claude Code 2.1.220 and types the required `dontAsk` permission mode directly.

`WebSocketTransport` is the Console-side custom Agent SDK transport. The sandbox runs the
`//haku/runtime/x/agent_sdk_transport:runner_bin` Python executable, which opens the WebSocket, starts a
configured local Claude Code executable, and copies newline-delimited JSON between the socket and
Claude's stdin/stdout. The runner imports no Agent SDK code.

## Framing

Every WebSocket frame is a **Haku envelope** — `{"kind": ..., "payload": {...}}` — and an SDK
stream-JSON message travels as one envelope's payload. Three kinds today: `start` (Console → runner,
once, first), `claude` (either direction), and `end_input` (Console → runner). `protocol.py` is the
whole definition.

The envelope exists so that Haku's control protocol and the SDK's conversation protocol do not share
a key namespace. They used to: Haku's frames were marked by `"type": "haku_transport"`, a reserved
value inside the _SDK's_ own `type` key, which holds only for as long as the SDK never emits that
value — and leaves nowhere to put a frame belonging to neither protocol.

The `start` payload carries the CLI arguments, working directory, and explicit environment produced
from Console's `ClaudeAgentOptions`, preserving dynamic options such as resume, system prompts, and
SDK MCP configuration without moving SDK orchestration into the sandbox. A compatibility test locates
the real Claude executable bundled in the pinned SDK wheel and verifies that the launch command
matches `SubprocessCLITransport` exactly.

**Gotcha: the two ends are separate images that roll independently.** Console ships in
`haku-console`; the runner ships in `haku-claude-runner`, whose tag the SandboxTemplate carries under
its own Flux image policy. A `PROTOCOL_VERSION` bump is therefore not atomic in production — for the
minutes between the two rollouts, sessions fail their first frame. That is loud and self-healing (the
supervisor replaces the session and announces it in the room), not silent, but it is the cost of
changing this file.

The runtime enables both partial messages and fine-grained tool streaming. Console therefore receives
incremental text and `input_json_delta` tool-argument events, followed by complete `ToolUseBlock` and
`ToolResultBlock` values for durable projections.

The runner accepts `--websocket-url` and `--claude-path`, and optionally sends the
`HAKU_AGENT_SDK_RUNNER_TOKEN` environment value as a bearer credential. That bridge credential is
removed from the child CLI environment. The sandbox image must make the pinned Claude executable
available at the configured path. This package
deliberately does not yet provision sandboxes, mount the transport in Haku Console, or expose tools.
Those are subsequent vertical slices once the executable transport boundary is covered independently.
