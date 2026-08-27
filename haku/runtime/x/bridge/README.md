# bridge — Haku's harness-opaque process bridge

The runner is deliberately thin. It launches the immutable harness selected by `--harness`, copies
native newline-delimited JSON between the harness stdio and the Console WebSocket, and retains an
ordered replay window. Claude and Codex share this runner OCI; harness selection remains explicit
and provider-specific behavior stays behind the backend seam.

| file                  | role                                              |
| --------------------- | ------------------------------------------------- |
| `protocol.py`         | incompatible v3 Pydantic envelope and negotiation |
| `transport.py`        | provider-neutral Console-side WebSocket transport |
| `client.py`           | provider-neutral client/sink value types          |
| `backend.py`          | process-launch seam                               |
| `backend_registry.py` | harnesses linked into the shared runner binary    |
| `claude_options.py`   | Claude launch material and executable             |
| `codex_options.py`    | Codex app-server launch material and executable   |
| `runner.py`           | sandbox process bridge                            |

Provider protocol clients live behind their Console adapters in `haku/console/x/claude_code/` and
`haku/console/x/codex_app_server/`.

## v3 framing

Every bridge frame is discriminated by the outer `kind`. Native harness JSON is opaque:

- `hello` — runner → Console negotiation
- `start` — Console → runner launch material and resume cursor
- `harness_frame` — the exact native harness JSON object in `frame`, either direction
- `end_input` — Console → runner stdin close
- `setup_output` — runner bootstrap/stderr bytes

The native frame's Claude `type`, Codex `method`, and other fields are never copied into the outer
kind or `session_frames.kind`. For example, the wire carries either
`{"kind":"harness_frame","seq":...,"frame":{"type":"assistant",...}}` or
`{"kind":"harness_frame","seq":...,"frame":{"method":"turn/completed",...}}`.
The database stores that exact native object in `session_frames.payload` for inspection/export.

Protocol v3 is intentionally incompatible. A runner that only advertises v2 has no common version,
so the Console refuses it and the runner exits/cleans up rather than guessing a framing contract.

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
