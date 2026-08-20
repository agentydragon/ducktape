# bridge — Haku's harness-opaque process bridge

The runner is deliberately thin. It launches the immutable harness selected by `--harness`, copies
native newline-delimited JSON between the harness stdio and the Console WebSocket, and retains an
ordered replay window. Claude is the only production harness in this change; Codex is a later
change.

| file                  | role                                              |
| --------------------- | ------------------------------------------------- |
| `protocol.py`         | incompatible v3 Pydantic envelope and negotiation |
| `transport.py`        | provider-neutral Console-side WebSocket transport |
| `client.py`           | provider-neutral client/sink value types          |
| `backend.py`          | process-launch seam                               |
| `backend_registry.py` | harnesses linked into the shared runner binary    |
| `options.py`          | Claude launch material and executable             |
| `runner.py`           | sandbox process bridge                            |

Claude's control protocol client lives behind the Console adapter in
`haku/console/x/claude_code/client.py`.

## v3 framing

Every bridge frame is discriminated by the outer `kind`. Native harness JSON is opaque:

- `hello` — runner → Console negotiation
- `start` — Console → runner launch material and resume cursor
- `harness_frame` — the exact native harness JSON object in `frame`, either direction
- `end_input` — Console → runner stdin close
- `setup_output` — runner bootstrap/stderr bytes

The native frame's Claude `type`, JSON-RPC `method`, and other fields are never copied into the
outer kind or `session_frames.kind`. For Claude, the wire shape is
`{"kind":"harness_frame","seq":...,"frame":{"type":"assistant",...}}`.
The database stores that exact native object in `session_frames.payload` for inspection/export.

Protocol v3 is intentionally incompatible. A runner that only advertises v2 has no common version,
so the Console refuses it and the runner exits/cleans up rather than guessing a framing contract.

## Position-based replay

The runner assigns dense outer `seq` values to every frame it puts on the wire, including native deltas
and notifications. `start.resume_from` is the highest sequence recorded for the session. The runner
retains and replays every frame above that cursor; no backend-specific `replayable()` classifier is
involved. The Console deduplicates by `(session_id, runner_seq)` and stores the original native frame,
direction, and timestamps.

`--harness claude` is required by the deployed Claude SandboxTemplate. The selected harness is
resolved once at runner startup and cannot change for the lifetime of the process. The claim gives
the sandbox one exact-session bearer. The runner uses it for the bridge and the Agent intentionally
inherits it for Console MCP, so native MCP support and ordinary HTTP clients such as `curl` exercise
the same pinned Agent/profile/binding authority. It is not the Claude OAuth credential, which never
enters the sandbox. Claims expose the same bearer as both `HAKU_AGENT_SDK_RUNNER_TOKEN` and the
rolling-compatible `HAKU_MCP_BEARER_TOKEN` alias: the previous runner strips only the first name,
while the new runner preserves both, so either rollout direction leaves Claude's MCP configuration
usable without creating a second credential.
