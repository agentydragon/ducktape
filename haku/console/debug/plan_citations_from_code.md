# De-citing the chat-runtime plans from code

**The problem this exists to fix.** A code comment citing a plan is a category error. Plans are
transient and empty out as their work lands, so anything code points at permanently is by definition
_not_ work left to do — it is a description of how the system behaves, and it belongs at the call
site, in <../x/README.md>, or in <../x/channels/matrix/SPEC.md>. Keeping a plan entry alive because
code points at it freezes an opaque identifier into the plan forever and stops the plan being a list
of what is left, which is the whole thing a plan is for.

Six chat-runtime plans merged into <../plans/conversation_layers.md> and
<../x/channels/matrix/SPEC.md>. That merge deliberately did not touch code, so **175 citations are
now stale**: 61 `<path>` links into a deleted file, and 114 `R<n>.<m>` tags naming requirements that
no longer exist under those numbers. A stale comment is not a build failure, so this can land before
or after the merge and need not be one PR — but it should be one sweep rather than a slow drift.

**Three answers, and none of them is "put the number back".**

1. **The comment should say the thing.** Most common. `(R3.6a)` becomes a sentence stating the
   invariant in the terms of that call site; the indirection into a 1200-line document was never
   doing the reader a favour.
2. **It is a shared contract several sites depend on** → state it once in the README or the SPEC and
   let the sites point at _that_, which is durable prose rather than a burn-down item.
3. **It is archaeology** — a requirement that is now simply how the system works, with nothing for a
   future editor to act on. Delete the comment.

Anything that still wants a plan pointer should get one at the section that carries the remaining
work, never at a step number, since steps are deleted as they land.

## A. `<path>` links into a deleted plan — 61 sites

Grouped by what the link was pointing at. The right-hand column is the recommended answer.

### The fold and its durable cursor — 8 sites

`chat_runtime_projection.md` § The shape, and § stage 3.

| Site                                                       | Answer                                                                                  |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `database_schema.py:1031` (`sessions.projected_frame_seq`) | (1) The cursor advances in the same transaction as the frame's effects. Say that.       |
| `database_schema.py:1194` (`session_turns` state columns)  | (1) Each column is written beside the effect it describes. Say that; drop § stage 3.    |
| `migrations/versions/0051_session_projection_cursor.py:6`  | (1) Same sentence, in the migration's own words.                                        |
| `migrations/versions/0044_turn_state.py:6`                 | (1) Same.                                                                               |
| `x/session_runtime.py:581`, `:755`                         | (2) <../x/README.md> § `session_store.py` and `session_runtime.py`                      |
| `x/session_store.py:290`, `:1127`                          | (2) Same.                                                                               |
| `x/claude_code/projection.py:6`                            | (1) `project(state, frames) -> (state, Projection)` is resumable from a cursor. Say it. |

### The neutral vocabulary and its provenance — 12 sites

`chat_runtime_projection.md` § stage 4, § "The projection is not a one-way door", § The four
interpreters counted, § Does a turn live over frames or over neutral events.

| Site                                                              | Answer                                                                                   |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `x/conversation_events.py:5`, `x/frame_projection.py:46`          | (2) <../x/README.md> § The neutral projection                                            |
| `x/room_status.py:9`, `x/setup_output.py:28`                      | (2) Same.                                                                                |
| `x/session_runtime.py:569`                                        | (2) Same.                                                                                |
| `chat_models.py:151`                                              | (1) The membership test the comment already states; the citation adds nothing.           |
| `frontend/x/session_frames_page.tsx:66`                           | (2) The carve-out lives in <../plans/conversation_layers.md> § 11; that section stays.   |
| `database_schema.py:1110`, `0046_anchor_message_frame_range.py:6` | (1) A projection nobody can appeal is one nobody can debug. Say it; it is the reason.    |
| `x/session_views.py:363`                                          | (3) All four interpreters are gone. Delete the citation.                                 |
| `0049_neutral_turn_usage.py:7`                                    | (3) Settled: the turn is neutral and its boundary is the adapter's. Delete the citation. |

### The frame log's two vocabularies and the runner's numbering — 14 sites

`chat_runtime_projection.md` § 2, § 2b. **This one is still unbuilt**, so a plan pointer is
legitimate: <../plans/conversation_layers.md> § 13.

`x/claude_code/frames.py:6`, `x/setup_output.py:11`, `database_schema.py:1379`, `:1407`,
`x/session_runtime.py:116`, `:392`, `runtime/x/bridge/runner.py:59`, `:92`,
`runtime/x/bridge/cli_client.py:66`, `:107`, `runtime/x/bridge/protocol.py:134`,
`migrations/versions/0050_frame_runner_seq.py:6`, `test_frame_runner_seq_migration.py:72`,
`cli_protocol/probes/compaction.py:4`.

Answer (2) for all: repoint to `§ 13`. `compaction.py:4` is the exception — it is citing "the
projection determines the transcript", which is a built property, so (1): say it.

### Reconciliation, the cursor, and lifecycle events — 9 sites

`session_channels.md` § 1, § 3, § 4.

| Site                                                           | Answer                                                                                  |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `database_schema.py:941` (`chat_attachment`), `:1473` (outbox) | (2) <../plans/conversation_layers.md> § 5 — still unbuilt, so a plan pointer is honest. |
| `x/channels/matrix/outbox.py:98`, `session.py:472`             | (2) Same.                                                                               |
| `0065_turn_aborted_event.py:5`, `0067_chat_delivery.py:6`      | (1) Each states its own reason already; the citation is decoration.                     |
| `x/channels/matrix/session.py:306`                             | (1) A `session_events` row names a session, so a rejection with no session has no home. |
| `x/channels/matrix/test_sync.py:347`                           | (1) Name the behaviour under test, not the section that argued for it.                  |
| `0059_prompt_session_events.py:5`                              | (1) The prompt is `authored` because no frame carries it at enqueue time. Already said. |

### Idle sessions, and the "Anytime" cleanups — 4 sites

`chat_runtime_cleanup.md` § Stage 6, § Anytime.

`chat_models.py:15` and `0054_session_idle_status.py:4` → (2) <../plans/conversation_layers.md>
§ 9 step 3, which is still open (#4231). `0036_frames_by_kind.py:10` and
`x/conversation_records.py:7` → (3): the growth they name is gone and the split they name has
happened.

### The legacy purge — 3 sites

`next_month.md` § 1. `0058_session_constraints_after_purge.py:3`,
`0062_frames_partial_default.py:5`, `0068_drop_unmapped_and_narrow_kinds.py:10`. Answer (2) for all:
<2026_08_16_legacy_purge.md> is the record of the run and is the durable home; the migrations should
point there.

### The Matrix channel itself — 11 sites

`matrix_chat_runtime.md`, with or without an R-number. Every one is behaviour, so answer (2):
<../x/channels/matrix/SPEC.md>.

`config.py:232`, `x/channels/matrix/client.py:17`, `formatted_body.py:5`,
`tools/conversations.py:4`, `database_schema.py:1368`, `:1640`, `chat_models.py:40`,
`runtime/x/bridge/protocol.py:185`,
`cluster/k8s/haku/console/deployment.yaml:110`,
`cluster/k8s/haku/console/matrix_system_prompt.md.j2:63`,
`cluster/provisioners/matrix_user_provisioner/provision_matrix_users.py:15`.

## B. `R<n>.<m>` citations — 114 sites

Every R-number is gone. What each described is either in the SPEC, or is a rule the call site should
state itself, or has been reversed.

### Now in the SPEC — answer (1) or (2)

The numbers below name behaviour that <../x/channels/matrix/SPEC.md> states in prose. A site with
one call site's invariant takes (1) — say it. A site that is one of several depending on the same
contract takes (2) — point at the SPEC section.

| Was           | Now, in the SPEC                                          | Sites                                                                                                                                         |
| ------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| R1.4          | § Ingress — enqueue succeeds with no sandbox              | `app.py:495`, `x/channels/matrix/session.py:10`                                                                                               |
| R1.5          | § Ingress — Haku's own events are never input             | `client.py:113`, `:180`, `:406`, `sync.py:390`, `test_client.py:84`, `:132`, `:147`                                                           |
| R1.6          | § Ingress — no inbound message silently dropped           | `client.py:198`, `:420`, `sync.py:14`, `:458`, `:474`, `:477`, `test_client.py:93`, `test_sync.py:365`, `test_homeserver_e2e.py:173`          |
| R1.7, R1.7a   | § Ingress — downtime recovery, and a first run's position | `client.py:454`, `sync.py:146`, `test_client.py:217`, `:257`, `test_homeserver_e2e.py:9`, `:129`, `:142`, `testing/console_deployment.py:212` |
| R2.1          | § Batching — wakeups coalesce into one turn               | `test_sync.py:249`                                                                                                                            |
| R3.1          | § The room and the session behind it                      | `session.py:510`                                                                                                                              |
| R3.3a         | § The room — re-awakening reads our own transcript        | `session.py:118`, `:416`, `system_prompt.py:55`, `claude_code/testing/stub_claude.py:46`, `test_fullstack_e2e.py:299`                         |
| R3.6          | § The room — invitation, and only the operator's          | `config.py:243`, `sync.py:208`, `:442`, `test_sync.py:445`, `:469`, `test_homeserver_e2e.py:89`, `:124`, `testing/operator_room.py:228`       |
| R5.3a         | § The agent's own view — reads are unscoped               | `tools/conversations.py:56`, `tools/recall_index.py:14`, `x/session_store.py:993`, `x/test_session_store.py:337`                              |
| R5.4a, R5.5   | § The agent's own view — the room is the smaller corpus   | `tools/conversations.py:4`, `database_schema.py:1368`                                                                                         |
| R6.1          | § What the room shows — the typing notice                 | `client.py:51`, `session.py:489`, `sync.py:288`, `session_runtime.py:714`, `test_room_status.py:203`, `test_homeserver_e2e.py:278`            |
| R6.2          | § What the room shows — the lazy status line              | `session.py:481`, `sync.py:244`, `room_status.py:29`, `test_room_status.py:127`                                                               |
| R6.3          | § What the room shows — coarse by rule                    | `room_status.py:57`, `test_room_status.py:72`                                                                                                 |
| R6.5          | § What the room shows — one line, edited and retired      | `client.py:333`, `:370`, `session.py:485`, `sync.py:244`, `:304`, `test_sync.py:602`, `test_homeserver_e2e.py:243`, `:261`                    |
| R7.1          | § What the room shows — every transition is announced     | `session.py:477`, `runtime/x/bridge/protocol.py:185`                                                                                          |
| R9.3          | § Credentials and identity — sender to Operator           | `config.py:248`, `session.py:534`                                                                                                             |
| R10.3, R10.3a | § Credentials — the console mints and caches its token    | `client.py:171`, `session.py:80`, `sync.py:197`, `test_sync.py:568`, `test_homeserver_e2e.py:96`, `provision_matrix_users.py:15`              |
| R10.3b        | § Credentials — the password Secret is a soft dependency  | `app.py:284`, `config.py:236`, `:398`, `sync.py:174`                                                                                          |
| R11.1         | § Replies — every assistant message is forwarded          | `sync.py:227`, `x/session_store.py:1169`                                                                                                      |
| R11.2         | § Replies — every turn speaks, no silence token           | `session.py:62`, `:399`, `:457`, `session_runtime.py:718`, `:726`, `test_session.py:373`, `test_session_runtime.py:1059`                      |
| R11.3, R11.3a | § The agent's own view; § The room — past sessions        | `client.py:115`, `chat_models.py:40`, `database_schema.py:1640`, `session.py:249`, `:409`, `test_session.py:459`                              |
| R11.6         | § Replies — a produced reply is never lost silently       | `database_schema.py:1467`, `test_fullstack_e2e.py:30`, `test_outbox.py:229`                                                                   |
| R11.7         | § Replies — a reply arrives formatted                     | `client.py:314`, `formatted_body.py:6`, `test_homeserver_e2e.py:217`                                                                          |

### Reversed or withdrawn — answer (1), and the comment is the thing to fix

These three name rules the system no longer holds. Every site citing one is either code the plan
schedules for deletion or a test pinning the new behaviour, so the comment should state what is true
now and, where relevant, that the code is going.

- **R2.2 / R2.5 — a mid-turn batch is held, and acknowledged only after its turn.** Reversed: a
  mid-turn prompt is rejected and acceptance is the acknowledgement. Sites:
  `test_sync.py:326` (already says "R2.2 reversed" — say what it tests instead),
  `test_fullstack_e2e.py:277`, `0043_matrix_held_batch.py:1`, `:13`,
  `0060_matrix_token_and_watermark.py:19`. The two migrations are history and take answer (3); the
  `0060` line should keep its point — re-delivery is survivable, a skip is not — without the tag.
- **R3.6a — one room per bot user, ever.** Withdrawn: one bot serves many rooms, and
  `chat_attachment`'s partial unique index expresses the rule that is actually wanted. Sites:
  `database_schema.py:1619`, `x/channels/matrix/session.py:167`, `:426`, `sync.py:3`, `:208`,
  `:216`, `:398`, `test_sync.py:413`, `:431`. **Every one of these is code
  <../plans/conversation_layers.md> § 9 step 6 deletes**, so the honest comment says what the code
  does today _and_ that the singleton is the thing being removed — not a citation into a plan whose
  step will be deleted when it lands.

### Design notes that were never behaviour — answer (3)

`R5.2a` in `cli_protocol_ownership.md` and `R5.5a` in prose are arguments about mechanisms that were
weighed and not taken. Nothing in code cites them; they are listed only so a search for them does
not look unfinished.

## Doing it

One PR, or a few by area — the Matrix channel, the runtime, the migrations — but not a trickle: a
half-de-cited file is worse than either state, because the next reader cannot tell which comments
were checked. The check afterwards is that both of these return nothing:

```bash
git grep -n 'R[0-9]\+\.[0-9]\+[a-z]\?' -- '*.py' '*.ts' '*.tsx'
git grep -n 'chat_runtime_projection.md\|one_read_api.md\|matrix_chat_runtime.md' \
          -- '*.py' '*.ts' '*.tsx' '*.yaml' '*.j2'
git grep -n 'chat_runtime_cleanup.md\|next_month.md\|session_channels.md' \
          -- '*.py' '*.ts' '*.tsx' '*.yaml' '*.j2'
```

Delete this note once they do.
