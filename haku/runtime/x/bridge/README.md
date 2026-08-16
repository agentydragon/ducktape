# bridge — the console's Claude CLI, at the far end of a WebSocket

The sandbox side of the Claude chat experiment, and the console-side client that talks to it. The
console owns the CLI's own newline-delimited JSON protocol (`cli_client.py`, described in
<../../../cli_protocol/README.md>); a Claude Code process runs inside a Haku sandbox pod; this
package is everything between them.

| file            |                                                                                      |
| --------------- | ------------------------------------------------------------------------------------ |
| `protocol.py`   | the Haku envelope, its version, and the `TextWebSocket` port                         |
| `transport.py`  | `WebSocketTransport` — the console end: launch, then frames both ways                |
| `cli_client.py` | `ClaudeCli` — the protocol client, both channels, and the `FrameSink` numbering them |
| `backend.py`    | `CliBackend` — which CLI is being run, and the `ProcessLaunch` that starts it        |
| `options.py`    | Claude as a backend: `ClaudeSession` → argv, and `ClaudeBackend` → the binary        |
| `runner.py`     | `runner_bin`, which runs **inside** the sandbox and copies bytes to the CLI's stdio  |

## Backends

The runner launches the backend it was told to launch (`--backend`, `HAKU_CLI_BACKEND`,
defaulting to `claude`) and pumps its stdio; nothing else in it names a CLI. A backend is the
three decisions that differ between CLIs — which argv, which binary, and which of the CLI's
frames survive being replayed to a console that adopts the session — and Claude is the one
implementation. What a second one would have to provide is <docs/second_backend.md>.

Each backend names its own executable variable rather than sharing one: `HAKU_CLAUDE_PATH` is
Claude's, set by `runner_image` below, and a second CLI ships in its own image with its own.

**Named for what it is.** It was `agent_sdk_transport`, after the Agent SDK whose `Transport`
interface `WebSocketTransport` used to implement. The SDK is gone from the code — the console drives
the CLI protocol itself — and what remained of the dependency was a build-time source for the
`claude` binary. Nothing here is an SDK transport, so nothing here is named after one.

## Framing

Every WebSocket frame is a **Haku envelope** — `{"kind": ..., "payload": {...}}` — and one CLI
protocol frame travels as one envelope's payload. Three kinds: `start` (console → runner, once,
first), `claude` (either direction), and `end_input` (console → runner). `protocol.py` is the whole
definition.

The envelope exists so that Haku's control protocol and the CLI's conversation protocol do not share
a key namespace. They used to: Haku's frames were marked by `"type": "haku_transport"`, a reserved
value inside the _CLI's_ own `type` key, which holds only for as long as the CLI never emits that
value — and leaves nowhere to put a frame belonging to neither protocol.

The `start` payload carries the argv, working directory and explicit environment `options.py` built,
so what the console launches is decided by the console and the runner adds nothing to it.

**Gotcha: the two ends are separate images that roll independently.** The console ships in
`haku-console`; the runner ships in `haku-claude-runner`, whose tag the SandboxTemplate carries
under its own Flux image policy. A `PROTOCOL_VERSION` bump is therefore not atomic in production —
for the minutes between the two rollouts, sessions fail their first frame. That is loud and
self-healing (the supervisor replaces the session and announces it in the room), not silent, but it
is the cost of changing that file.

The launch enables both partial messages and fine-grained tool streaming, so the console receives
incremental text and `input_json_delta` tool-argument events as well as the completed `assistant`
frames it writes down.

The runner accepts `--websocket-url`, `--backend` and `--cli-path`, and optionally sends the
`HAKU_AGENT_SDK_RUNNER_TOKEN` environment value as a bearer credential — a name this rename
deliberately left alone, because it is a deploy contract shared with a SandboxTemplate, and a live
session's runner pod keeps its image for up to `session_ttl_seconds`, so "new console, old runner"
outlives any console roll. That bridge credential is removed from the child CLI's environment. The
sandbox image must make its backend's executable available at the path that backend's variable names.
