# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

The session/conversation runtime that used to live here has graduated: the durable record to
<../conversation/>, the runner-incarnation machinery to <../session/>, and the wake wires to
<../notifications/> (#4772; target layout: <../docs/naming_and_layout.md> § 2). What remains is
what is still being restructured:

| Where                                   | What it is                                                                                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conversation_events.py`                | The provider-neutral vocabulary a conversation is read as; deletion-scheduled with the #4667 native-projector fold.                                                       |
| `session_events.py`                     | The stream's two categories as stored rows — the one place the vocabularies meet the table. Merges into `conversation/conversation_event.py` (#4772 C5).                  |
| `runtime.py`, `runtime_catalog.py`      | Backend-neutral runtime catalog and its application composition; the harness _selection_ residue headed for `harnesses/` (<../docs/naming_and_layout.md> § 2).            |
| `x/channels/<name>/`                    | **One channel.** Matrix today; the SPA is served by the runtime's own routes.                                                                                             |
| `x/claude_code/`, `x/codex_app_server/` | **One CLI harness each.** Named for the product whose binary they launch, not for the model. The native client + projection move runner-ward (#4667, deletion-scheduled). |
| `testing/`                              | Stand-ins a test stands something up _with_, importable by non-pytest processes too (<testing/recording_claims.py> for the rationale).                                    |
| `conftest.py`                           | Fixture re-registrations for the tests below this directory; the definitions live in <../session/conftest.py>.                                                            |

How to place a module — the replace-the-other-axis test and the boundary cases — is
<../docs/chat_layers.md> § Placing something new.

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

## Tests

The runtime-level fixture definitions live in <../session/conftest.py> (see
<../session/README.md> § Tests run against a real database for the no-fake-stores rule);
<conftest.py> re-registers them for the harness, channel, and e2e tests below this directory.
The homeserver identities, the config they compose into and the room binding live in
<channels/matrix/conftest.py> — nothing channel-shaped may leak into the shared fixtures.

The e2e tiers, each with a module docstring saying what only it can answer:

- <test_generation_cutover_e2e.py> — the post-cut stack end to end: a real runner process on a
  real websocket journaling to a real Console handler — the generation window's health gate.
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
- `Session`, `Conversation`, `ChannelAttachmentRow`, `MatrixAccessToken` and `MatrixSyncWatermark`
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
