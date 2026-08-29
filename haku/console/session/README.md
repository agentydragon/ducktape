# haku/console/session — one runner incarnation and its wire log

A session is one runner's life: its sandbox, its lease, its turns, and the wire frames it
exchanged. What a session may never do is name a channel — the layer contract is
<../docs/chat_layers.md>. Graduated from `../x/` under #4772; the target layout is
<../docs/naming_and_layout.md> § 2.

The shared substrate is two files, and the line between them is the transaction: `store.py`
holds the rows and every method whose job is "these writes commit together or not at all"
(`apply_frame` is the one to read first), and `runtime.py` drives one turn against a CLI — the
turn loop, the runner's websocket bridge, the sandbox lifecycle, and the SPA chat surface's own
HTTP routes. Around them:

| Module                  | Role                                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `conversation_views.py` | The SPA's wire shapes — inventory, conversation detail, follow messages — as projections over `../conversation/reads.py`. |
| `sandbox_allocation.py` | Elected reconciler (`SBOX`): durable prompt demand into sandbox allocation, independent of every channel.                 |
| `sandbox_claims.py`     | The declarative `SandboxClaim` one session runs in, and the claim/Sandbox/Pod/runner progress view.                       |
| `subscription.py`       | Reading a conversation from a position; where the position lives is the subscriber's own (`Cursor`).                      |
| `system_prompt.py`      | The system prompt a chat session is started with (the template is deploy config).                                         |
| `launch_identity.py`    | Neutral launch-identity types shared by channel and runtime stores.                                                       |
| `setup_output.py`       | The bridge envelope's `kind`; the setup-narration compatibility frame.                                                    |
| `session_frames.py`     | Vocabulary of the durable session wire log (`session_frames` rows).                                                       |
| `status.py`             | The session lifecycle vocabulary: status, the status sets, and how a lease can fail one.                                  |

The elected loops here — runtime supervision (`CRUN`) and allocation (`SBOX`) — hold
independent advisory locks and can land on different replicas, so a stalled claim cannot wedge
ingress or make one channel the only surface able to recover durable demand. The Matrix sync
loop (`MXSY`) is a third such election, held by the separately deployed `haku-matrix-adapter`
worker (<../channels/matrix/worker.py>).

**Cross-replica state, and the trap it sets:** `replicas: 2` means any given HTTP request
reaches an arbitrary pod, while a session's live objects — the runner's bridge websocket, its
abort event — belong to exactly one. Anything that has to reach a running turn therefore goes
through Postgres `NOTIFY`, never an in-process registry: a dict keyed by session id looks
correct in tests and single-replica dev, and silently answers "no such session" in production
about half the time. `Store.request_abort` (`store.py`) is the shape to copy.

## Operator surfaces

- **Frame inspector** — `GET /api/sessions/{session_id}/frames`, rendered by
  <../frontend/x/session_frames_page.tsx> at `/_console/sessions/<id>/frames`: one page of the
  rollout in wire order, payloads whole. `conversation_item` is a lossy projection of
  `session_frames`, and a projection nobody can appeal is a projection nobody can debug. It is
  the one surface that shows a backend's own wire, and it stays safe by being addressed
  separately, never load-bearing, and labelled as one backend's wire everywhere a reader sees
  it (`conversation_views.SessionFrameView`, `Store.read_operator_frames`).
- **Provisioning** — `GET /api/sessions/{session_id}/provisioning`: the claim/Sandbox/Pod/runner
  graph for one session in whatever state it is now (`SessionService.sandbox_provisioning`).
- **Composer** — the conversation detail view carries <../frontend/x/conversation_composer.tsx>
  for any session it can read, a room's included; the reply goes wherever that session's
  channel sends replies, so a prompt typed in the browser also lands in the room.

### SandboxClaim bearer boundary

The allocator puts the session's random `HAKU_RUNNER_TOKEN` directly in the runtime
`SandboxClaim.spec.env`. This is currently the upstream Agent Sandbox API's only way to pass the
claim-local environment: its `EnvVar` does not yet offer Secret-backed `valueFrom` injection. The
same bearer authenticates the runner bridge, Console MCP, and the HTTP egress proxy, so this claim
field is an authority-bearing handoff, not ordinary launch configuration.

This is load-bearing security policy. Runtime `SandboxClaims` must not become a generally readable
secret store: ordinary agents and sandbox service accounts must not get `get`, `list`, or `watch`
access to them. The Console's narrowly scoped claim access and the dedicated cleanup/controller
machinery are the intended readers; any new operator surface, debug tool, RBAC rule, or agent API
that reads claims must be reviewed as a bearer disclosure. Keep the bearer out of launch frames,
argv, logs, and persisted session data; only its database fingerprint is retained.

**Gotcha:** both chat surfaces run at once as ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather than
contend. That also means two live sandboxes, and only the Matrix one announces itself, so the
browser one is the easy one to forget you are paying for.

## Tests run against a real database

Every store here is exercised through Postgres (the `migrated_*` testcontainer fixtures),
never a stand-in. What stays faked is what is genuinely outside: Kubernetes and the CLI. The
rule is not tidiness — a fake store answers from the shape the test author imagined, so it
agrees with whatever the code does: a fake `_listen` written against psycopg3's API passed
every test while the real one raised on **every** call in production against the asyncpg
engine, and a fake conversation store let a test bind a room to a session id that had never
existed, a scenario the schema's foreign key refuses.

<conftest.py> holds the runtime-level fixtures — the stores, the service, the claim stand-in
and the operator's identity — and nothing a room knows; a second channel has to inherit them
unchanged. The conversation, notifications, and `../x/` conftests re-register them for their
own trees; the stand-ins themselves live in <../x/testing/> so a non-pytest process can reach
them too.
