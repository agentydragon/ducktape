# bridge — the console's Claude CLI, at the far end of a WebSocket

The sandbox side of the Claude chat experiment, and the console-side client that talks to it. The
console owns the CLI's own newline-delimited JSON protocol (`cli_client.py`, described in
<../../../cli_protocol/README.md>); a Claude Code process runs inside a Haku sandbox pod; this
package is everything between them.

| file            |                                                                                      |
| --------------- | ------------------------------------------------------------------------------------ |
| `protocol.py`   | the Haku envelope, its version, and the `TextWebSocket` port                         |
| `transport.py`  | `WebSocketTransport` — the console end: negotiate, launch, then frames both ways     |
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

**The Agent SDK is not in this runtime.** The console drives the CLI protocol itself; the SDK
survives only as a build-time source for the `claude` binary, which `extract_claude.py` pulls out of
the repository-pinned wheel.

## Framing

Every WebSocket frame is a **Haku envelope**, discriminated on `kind`, and one CLI protocol frame
travels as one `claude` envelope's `payload`. Five kinds: `hello` (runner → console, first on every
connection, carrying the versions it speaks), `start` (console → runner, its reply to `hello` and
its own first frame), `claude` (either direction), `end_input` (console → runner), and
`setup_output` (runner → console, the sandbox bootstrap's raw bytes). `protocol.py` is the whole
definition.

The envelope exists so that Haku's control protocol and the CLI's conversation protocol do not share
a key namespace. Marking Haku's frames inside the _CLI's_ own `type` key would hold only for as long
as the CLI never emits the reserved value, and would leave nowhere to put a frame belonging to
neither protocol — which `setup_output` is.

The `start` payload carries the argv, working directory and explicit environment `options.py` built,
so what the console launches is decided by the console and the runner adds nothing to it.

## Numbering, and what it makes catch-up

**The runner numbers every frame it sends** — `seq`, dense and monotonic for the life of the session,
assigned where the frame goes on the wire so a re-sent one keeps the number it first went out under.
It is the peer's fact, so the peer can act on a cursor built from it, and it is dense, so a hole in
it is evidence of loss — which a database sequence, sparse by design, can never be.

The console **records** that number today (`session_frames.runner_seq`) and computes its cursor from
it. Making it the log's own ordering is the schema step after, and the reason for the whole scheme
(<../../../console/plans/conversation_layers.md> § 13).

`start` carries the other half, `resume_from`: the highest `seq` the **console** holds for this
session. The runner replays only what is above it and continues numbering from there. Per session
rather than per connection, since two consoles can be adopting one runner's window at once during a
roll and each computes the same cursor from the same rows. Without it — an older console, or one
with nothing recorded — the whole window is offered and deduplicated at the far end, which is what
every adoption used to get.

The counter lives in the runner and not in `WebSocketTransport` because the runner is the end that
survives: the console is replaced on every roll, while `run()` holds the CLI across as many sockets
as that takes.

**Deviation worth knowing: both fields are optional additions rather than a `PROTOCOL_VERSION`
bump**, for the reason the gotcha below gives. `extra="ignore"` means a peer predating them drops
them and behaves exactly as it did, in both directions of skew.

**Gotcha: the two ends are separate images that roll independently.** The console ships in
`haku-console`; the runner ships in `haku-claude-runner`, whose tag the SandboxTemplate carries
under its own Flux image policy. A `PROTOCOL_VERSION` bump is therefore not atomic in production —
for the minutes between the two rollouts, sessions fail their first frame. That is loud and
self-healing (the supervisor replaces the session and announces it in the room), not silent, but it
is the cost of changing that file.

The launch enables both partial messages and fine-grained tool streaming, so the console receives
incremental text and `input_json_delta` tool-argument events as well as the completed `assistant`
frames it writes down.

The runner accepts `--websocket-url`, `--session-id`, `--backend`, `--cli-path` and `--setup-path`,
and optionally sends the `HAKU_AGENT_SDK_RUNNER_TOKEN` environment value as a bearer credential.
That variable keeps its Agent-SDK-era name because it is a deploy contract shared with a
SandboxTemplate, and a live session's runner pod keeps its image until the session ends —
`_renew_lease` slides the sandbox's `shutdownTime` on every heartbeat, so it does not age out —
which makes "new console, old runner" outlive any console roll. That bridge credential is removed
from the child CLI's environment. The sandbox image must make its backend's executable available at
the path that backend's variable names.
