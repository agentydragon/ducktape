# bridge — Haku's harness runner

The runner launches the immutable harness selected by `--harness` and serves it to whichever Console
is up, across as many reconnections as a session takes. The harness speaks its native protocol and
projects it to the backend-neutral conversation operations of `neutral_operations.py`; the runner
owns only the harness-invariant lifecycle — dial, the two handshakes, the roll replay — and never
interprets a native payload or drives a turn. Claude and Codex share this runner OCI; harness
selection is explicit and each harness's whole run-loop lives behind the `run()` seam.

| file                                        | role                                                        |
| ------------------------------------------- | ----------------------------------------------------------- |
| `protocol.py`                               | incompatible v4 Pydantic envelope and negotiation           |
| `neutral_operations.py`                     | the backend-neutral conversation-operation vocabulary       |
| `operation_journal.py`                      | the runner's ACK-gated batch journal                        |
| `session_api.py`                            | one session's numbering, retention, journal, and admission  |
| `communicator.py`                           | the Console-facing transport: dial, handshakes, roll replay |
| `backend.py`                                | the `Harness` seam and the shared subprocess-stdio pump     |
| `backend_registry.py`                       | harnesses linked into the shared runner binary              |
| `projection.py`                             | the neutral projection yield and shared fold helpers        |
| `claude_harness.py` / `claude_options.py`   | Claude's run-loop and its launch material                   |
| `claude_projection.py`                      | Claude native frames → neutral operations                   |
| `codex_harness.py` / `codex_options.py`     | Codex's run-loop and its launch material                    |
| `codex_projection.py` / `codex_protocol.py` | Codex notifications → neutral operations, and JSONL parsing |
| `runner.py`                                 | the harness-invariant lifecycle and process entry           |

Each harness interprets its own native frames runner-side. The Console keeps its provider adapters
(`haku/console/x/claude_code/`, `haku/console/x/codex_app_server/`) for launch selection and the
durable frame record; the runner-side projection is what the neutral generation commits. `client.py`
and `transport.py` are the Console-side client value types and WebSocket transport of the v3 path,
retained while that cutover completes.

## v4 framing

Every bridge frame is discriminated by the outer `kind`. A native harness frame stays opaque on the
wire, and the conversation itself crosses as the acknowledged neutral-operation journal:

- `hello` / `start` — runner → Console version negotiation, then the launch and resume cursor
- `harness_frame` — the exact native harness JSON object in `frame`, either direction — the durable
  record beside the journal, no longer a projection input
- `journal` — the neutral-operation journal, both directions: the runner's `RunnerHello` then
  `OperationBatch`es, the Console's `ConsoleResume` then `BatchAck`s
- `prompt` / `interrupt` — Console → runner: dispatch a pending prompt by durable id, or stop the turn
- `setup_output` — runner bootstrap/stderr bytes

The native frame's Claude `type`, Codex `method`, and other fields are never copied into the outer
kind or `session_frames.kind`. The database stores that exact native object in `session_frames.payload`
for inspection/export.

Protocol v4 is intentionally incompatible with v3. A v3 peer shares no common version, so the Console
refuses it and the runner exits/cleans up rather than guessing a framing contract; the `journal`
handshake doubles that gate on the migration-set generation.

## Position-based replay

The runner assigns dense outer `seq` values to every frame it puts on the wire, including native deltas
and notifications. `start.resume_from` is the highest sequence recorded for the session. The runner
retains and replays every frame above that cursor; no backend-specific `replayable()` classifier is
involved. The Console deduplicates by `(session_id, runner_seq)` and stores the original native frame,
direction, and timestamps.

`--harness claude` (or `codex-app-server`) is required by the deployed SandboxTemplate. The
selected harness is resolved once at runner startup and cannot change for the lifetime of the process.
The claim gives
the sandbox one exact-session bearer, `HAKU_AGENT_SDK_RUNNER_TOKEN`. The runner uses it for the
bridge and the Agent intentionally inherits it for Console MCP, so native MCP support and ordinary
HTTP clients such as `curl` exercise the same pinned Agent/profile/binding authority. It is not the
Claude OAuth credential, which never enters the sandbox.

When the Console launch environment contains `HAKU_KUBERNETES_PROXY_URL`, the runner creates
`$HOME/.kube/config` and a mode-0600 `$HOME/.kube/haku-agent-token`. The config points only at that
URL and references the token via client-go's `tokenFile`; bearer bytes never occur in argv,
or kubeconfig YAML. The URL must be https — client-go reads kubeconfig credentials only for a TLS
server, so a plain-http proxy receives every kubectl request unauthenticated — and the cluster
entry pins the launch-selected `SSL_CERT_FILE` trust bundle as its `certificate-authority`. They are intentionally present in the ephemeral SandboxClaim environment for
bridge/MCP authentication. The proxy is therefore Console-selected and authorization-aware, while
the runner has no direct ServiceAccount token. The Console must add
this launch variable when a session is permitted Kubernetes access; the runner intentionally has
no hard-coded proxy hostname. During rollout the existing Claude SandboxTemplate still mounts its
legacy ServiceAccount token for old runners and claims, even though new launches select this
kubeconfig. The follow-up removes that direct credential only after the proxy path is live.

`HAKU_RUNNER_SETUP` is likewise selected in the received launch environment. An explicit empty
value disables setup for an Agent whose workspace starts empty. The process-level image value
remains only as a rolling fallback for old Console replicas that launch Haku without this field.
