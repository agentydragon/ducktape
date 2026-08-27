# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

Two chat surfaces live here, deliberately. They are separate experiments over one piece of
session machinery, not a migration in progress: Matrix is not replacing the SPA view. Both are
headed for one subscription off one record (<../plans/conversation_layers.md>).

This README is the map. The contracts live on the code: each module's docstring states what it
owns and the invariants it keeps, and the map does not restate them.

## The directory says which axis a module varies on

Three things vary independently here:

| Where                                   | What it is                                                                                   |
| --------------------------------------- | -------------------------------------------------------------------------------------------- |
| `x/*.py`                                | **The runtime.** Sessions, turns, frames, notifications, sandboxes — no channel, no harness. |
| `x/channels/<name>/`                    | **One channel.** Matrix today; the SPA is served by the runtime's own routes.                |
| `x/claude_code/`, `x/codex_app_server/` | **One CLI harness each.** Named for the product whose binary they launch, not for the model. |

How to place a module — the replace-the-other-axis test and the boundary cases — is
<../docs/chat_layers.md> § Placing something new.

## The runtime level

The shared substrate is two files, and the line between them is the transaction:
`session_store.py` holds the rows and every method whose job is "these writes commit together
or not at all" (`apply_frame` is the one to read first), and `session_runtime.py` drives one
turn against a CLI — the turn loop, the runner's websocket bridge, the sandbox lifecycle, and
the SPA chat surface's own HTTP routes. Around them:

| Module                         | Role                                                                                                                                                                                                                                                                             |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conversation_events.py`       | The provider-neutral vocabulary a conversation is read as; every surface renders it, every backend adapter produces it.                                                                                                                                                          |
| `conversation_log.py`          | The only writer of `conversation_event`/`conversation_item`/`conversation_turn`: the log first, the entities from it, one transaction.                                                                                                                                           |
| `session_events.py`            | The stream's two categories as stored rows — the one place the vocabularies meet the table.                                                                                                                                                                                      |
| `subscription.py`              | Reading a conversation from a position; where the position lives is the subscriber's own (`Cursor`).                                                                                                                                                                             |
| `conversation_follow.py`       | `WS /api/conversations/{id}/follow`: a conversation's state and the changes to it, as one operation.                                                                                                                                                                             |
| `conversation_live_updates.py` | Conversation changes as console-socket invalidations for open tabs.                                                                                                                                                                                                              |
| `conversation_history.py`      | The finished conversation tail handed to a replacement session.                                                                                                                                                                                                                  |
| `conversation_reads.py`        | What a conversation read hands back, and the cursors that page them (see below).                                                                                                                                                                                                 |
| `item_entries.py`              | The one place a materialised item row folds onto `conversation_reads.py`'s entry union — one entry per row, identical for the MCP reader and the SPA views.                                                                                                                      |
| `reprojection.py`              | Re-project a recorded session's frames and report where the stored log disagrees.                                                                                                                                                                                                |
| `pg_wake.py`                   | The layer-neutral `LISTEN`/`NOTIFY` transport: `notify_raw`/`libpq_dsn` and the `WakeListener` connection, reconnect loop, parse and reconnect-gap dispatch, instantiated once per layer. The console's other LISTEN consumer is <../console_events.py>.                         |
| `session_wakes.py`             | The session layer's wake channel (`session_events`) and its surface: `SessionWakes` (`wait`/`watch_session`/`watch`) over its own `WakeListener`; consumed by `SessionService` and the allocator, and nothing conversation-shaped reaches it.                                    |
| `conversation_wakes.py`        | The conversation layer's wake channel (`conversation_wakes`) and its surface: `ConversationWakes.watch` over its own `WakeListener`, plus the cross-layer `notify_update`; a channel imports this and so names a conversation surface, never a session type (roll gotcha below). |
| `conversation_runtime.py`      | Elected reconciler (`CRUN`): conversation-owned prompt demand into sessions, plus global lease/claim maintenance.                                                                                                                                                                |
| `sandbox_allocation.py`        | Elected reconciler (`SBOX`): durable prompt demand into sandbox allocation, independent of every channel.                                                                                                                                                                        |
| `sandbox_claims.py`            | The declarative `SandboxClaim` one session runs in, and the claim/Sandbox/Pod/runner progress view.                                                                                                                                                                              |
| `conversation_views.py`        | The SPA's wire shapes — inventory, conversation detail, follow messages — as projections over `conversation_reads.py`.                                                                                                                                                           |
| `setup_output.py`              | The bridge envelope's `kind`; the setup-narration compatibility frame.                                                                                                                                                                                                           |
| `system_prompt.py`             | The system prompt a chat session is started with (the template is deploy config).                                                                                                                                                                                                |
| `launch_identity.py`           | Neutral launch-identity types shared by channel and runtime stores.                                                                                                                                                                                                              |
| `runtime.py`                   | Backend-neutral runtime catalog: provider adapters plus per-Agent execution resources.                                                                                                                                                                                           |
| `runtime_catalog.py`           | Application composition of the runtime implementations linked into the console.                                                                                                                                                                                                  |

The elected loops — Matrix sync (`MXSY`), runtime supervision (`CRUN`), allocation (`SBOX`) —
hold independent advisory locks and can land on different replicas, so a stalled claim cannot
wedge ingress or make one channel the only surface able to recover durable demand. The Matrix
leader also sweeps one reconciler per live attachment (subscriber, drain, send budget), which is
what makes each of those singular cluster-wide without an election of its own
(<channels/matrix/attachment_reconciler.py>).

**Gotcha:** both chat surfaces run at once as ordinary separate sessions — separate rows,
separate sandboxes — so a browser conversation and the Matrix conversation coexist rather than
contend. That also means two live sandboxes, and only the Matrix one announces itself, so the
browser one is the easy one to forget you are paying for.

**Cross-replica state, and the trap it sets:** `replicas: 2` means any given HTTP request
reaches an arbitrary pod, while a session's live objects — the runner's bridge websocket, its
abort event — belong to exactly one. Anything that has to reach a running turn therefore goes
through Postgres `NOTIFY`, never an in-process registry: a dict keyed by session id looks
correct in tests and single-replica dev, and silently answers "no such session" in production
about half the time. `SessionStore.request_abort` is the shape to copy.

### Operator surfaces

- **Frame inspector** — `GET /api/sessions/{session_id}/frames`, rendered by
  <../frontend/x/session_frames_page.tsx> at `/_console/sessions/<id>/frames`: one page of the
  rollout in wire order, payloads whole. `conversation_item` is a lossy projection of
  `session_frames`, and a projection nobody can appeal is a projection nobody can debug. It is
  the one surface that shows a backend's own wire, and it stays safe by being addressed
  separately, never load-bearing, and labelled as one backend's wire everywhere a reader sees
  it (`conversation_views.SessionFrameView`, `SessionStore.read_operator_frames`).
- **Provisioning** — `GET /api/sessions/{session_id}/provisioning`: the claim/Sandbox/Pod/runner
  graph for one session in whatever state it is now (`SessionService.sandbox_provisioning`).
- **Composer** — the conversation detail view carries <../frontend/x/conversation_composer.tsx>
  for any session it can read, a room's included; the reply goes wherever that session's
  channel sends replies, so a prompt typed in the browser also lands in the room.

### `conversation_reads.py` runs stable → experimental

It is the one edge in that direction, worth knowing before it surprises: everything else
outside `x/` that names a module inside it is the composition root (<../app.py>), while
<../tools/conversations.py> imports these models because the store is what produces them. The
module is a leaf of Pydantic models with no dependency of its own, so naming it drags in no
runtime — but if the tools catalog ever has to build without `x/`, moving this module to the
console level is the fix, not reinstating the models on the tool.

### Renaming a wake channel or payload

The Deployment rolls with `maxUnavailable: 0`, so old and new replicas run together for the
length of a roll. A renamed channel means the new replica notifies where the old one is not
listening, and the wakes are lost for that window — the same expand/contract discipline a
destructive migration needs. Notify on both names for one release, then drop the old, and gate
that second release on the roll having **converged** (every pod on an image at or after the
first) rather than on a release having elapsed, since `maxUnavailable: 0` means a bad image
stalls the roll with the old replica still serving. An explicit operator cutover under the
standing conversation-disruption allowance (<../AGENTS.md>) may skip the overlap, costing wake
latency for one roll and no data.

The trap in the overlap phase: while both names are being notified, every wake is delivered
twice, so a woken waiter proves nothing about which name woke it. Tests and production alike
will look healthy with the new path entirely broken, right up until the old one is deleted.
Cover the new path end to end on its own before contracting — a test driving `pg_notify` on
exactly one channel.

## Harness adapters

`claude_code/` speaks Claude Code's newline-delimited JSON: `frames.py` (the CLI's own `type`
vocabulary and the readers that pick a value out of one), `client.py` (the protocol client),
`projection.py` (the reducer into the neutral vocabulary — its contract is its own docstring;
read <../../cli_protocol/protocol.md> and the adjacent fixtures before changing it),
`runtime.py` (the adapter), `wake.py` (classifying idle-time frames), and `redaction.py` with
`frame_export.py`/`frame_export_main.py` — recording a production session as a redacted JSONL
fixture, how-to in <claude_code/frame_export_main.py>.

`codex_app_server/` is the second harness, over Codex's app-server JSON-RPC: the same
client/frames/projection/runtime split, plus `capture.py` for recording sanitized fixtures off
a real Codex.

## Matrix chat surface — `channels/matrix/`

- `client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `sync.py` — logs in as `@haku`, long-polls `/sync` (one owner for the user-wide token), binds
  each room the operator invites Haku into, and dispatches inbound events by room to their
  attached conversations. Holds the only Matrix credential, so everything that speaks into a
  room speaks through it; hosts the per-attachment reconcilers on the sync leader.
- `conversation.py` — each room's attachment to a conversation and ingress (`MatrixTurns`).
- `attachment_reconciler.py` — one owner per live attachment: its conversation cursor, reply
  outbox, span revisions and send budget; the sync leader sweeps the set per pass.
- `pacer.py` — one paced outbound queue per room, over Synapse's `rc_message` budget, addressed
  by the attachment (`RoomPacers`).
- `outbox.py` — the rooms' outbox: replies as `matrix_outbox` rows, and one drain per
  attachment that says them.
- `outbox_wake.py` — the outbox's own wake wire: the enqueue's transaction waking the drains.
- `revisions.py` — which homeserver event the channel is currently editing for a revisable
  subject.
- `spans.py` — the editable lines as spans of the conversation: the pure fold from the stream
  to each span's bounded body, its close, and the reconcile latches over a `RoomFrontend`.
- `conversation_subscriber.py` — the Matrix channel's subscriber to the conversation record,
  one per attachment: its durable position (`channel_cursor`), the replies it queues, the
  notices it seals, the span lines it reconciles.
- `room_copy.py` — the room's durable copy of projected events, read off their `/sync` echoes.
- `ingress_ledger.py` — which inbound events a prompt in the record carries.
- `formatted_body.py` — Haku's Markdown into the HTML subset Matrix clients render.

**The room reads the record; the turn loop never pushes at it.** The subscriber's module
docstring (<channels/matrix/conversation_subscriber.py>) is that contract; what the channel
guarantees the operator is <channels/matrix/SPEC.md>.

## Tests run against a real database

Every store here is exercised through Postgres (the `migrated_*` testcontainer fixtures),
never a stand-in. What stays faked is what is genuinely outside: Kubernetes and the CLI. The
rule is not tidiness — a fake store answers from the shape the test author imagined, so it
agrees with whatever the code does: a fake `_listen` written against psycopg3's API passed
every test while the real one raised on **every** call in production against the asyncpg
engine, and a fake conversation store let a test bind a room to a session id that had never
existed, a scenario the schema's foreign key refuses.

### The runtime's conftest names no channel

`conftest.py` is inherited downwards, so a runtime-level fixture that reached into
`channels/matrix/` would make every runtime test depend on a homeserver's vocabulary and a
second channel unaddable without dragging Matrix along. <conftest.py> holds the stores, the
service, the claim stand-in and the operator's identity — nothing a room knows; the homeserver
identities, the config they compose into and the room binding live in
<channels/matrix/conftest.py>. Each file's docstring carries its own half.

### The stand-ins live in `testing/`

Everything a test stands something up _with_ is a module in a `testing/` directory under the
axis it stands in for — never a `conftest.py` fixture or a source file in a target's `data` —
so a stand-in cannot outlive the thing it fakes and a `py_binary` can reach it
(<testing/recording_claims.py> for the rationale). The e2e tiers, each with a module docstring
saying what only it can answer:

- <test_bridge_e2e.py> — the Claude bridge end to end: a real runner process on a real
  websocket.
- <channels/matrix/test_homeserver_e2e.py> — `MatrixClient` against a real Synapse, for the
  properties of Synapse a canned response could only agree with.
- <channels/matrix/test_fullstack_e2e.py> — that Synapse, console replicas as processes, a
  runner per sandbox behind the stub `claude` (<claude_code/testing/stub_claude.py>), and a
  real Postgres: every message the operator sent has exactly one reply in the final room.

## What necessarily lives outside this directory

The stable modules own these, so moving them here is not possible without inverting the
dependency:

- `MatrixConfig` and `Settings.matrix` in <../config.py>. Absent config, or a config whose
  reflected bot password has not landed yet, means the surface does not start and the console
  does.
- `Session`, `Conversation`, `ChatAttachment`, `MatrixAccessToken` and `MatrixSyncWatermark`
  in <../database_schema.py>, plus their Alembic revisions — migrations are one lineage for
  the whole database.

## Where the reasoning lives

The code keeps the invariant; the evidence behind it is linked rather than restated.

- <../docs/chat_layers.md> — what a session, a conversation and a channel each own, the two
  edges between them, and where a new module, table, port or event kind goes. Read it before
  adding one.
- <../docs/chat_runtime_facts.md> — behaviours of Synapse, nio, uvicorn and the CLI that this
  surface depends on, with where each was checked. Read it before changing anything that looks
  like belt and braces.
- <../plans/conversation_layers.md> — what is still wrong and the order to fix it, and
  <channels/matrix/SPEC.md> — what the Matrix channel already guarantees.
