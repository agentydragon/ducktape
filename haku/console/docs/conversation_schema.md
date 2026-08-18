# The conversation record

What a thread is, as tables: the log every fact is written to, the entities materialised from it,
the state each channel keeps beside it, and the single migration that reaches this shape.

<chat_layers.md> holds the layer invariant — a channel talks only to the conversation, a session
talks only to the conversation, never to each other — and is not restated here. This is the schema
that invariant implies, plus the evidence that its vocabulary belongs to no one backend.

## 1. The vocabulary

### The test

A concept belongs at the conversation layer if it is **universal** — every backend worth adapting
reports it, in its own spelling — or **ours**, a console fact no runner reports at all. A concept
that is one backend's shape promoted upward belongs at the session layer, behind the adapter.

Neutrality is not established by asserting it, so every item below names what two other backends
emit for it: the **Codex app server** (`turn/*`, `item/*`, `thread/*` JSON-RPC notifications) and
the **OpenAI Responses API** (`response.*` streaming events).

**The two are not peers, and the difference decides several cases.** Codex's app server and Claude
Code's CLI are _harness_ protocols: something runs tools and reports what happened. The Responses
API is a _model_ API: it asks for the next output and stops. Our runner is a harness, so Codex is
the like-for-like test and the Responses API is the stress test. A concept the Responses API lacks
is admissible when both harnesses report it; a concept only Claude Code reports is not.

### Both stream-native protocols converged on one shape

```text
thread
└── turn                        started → … → completed, with a terminal status
    └── item                    a type, and a stable id deltas are addressed to
        └── started → deltas → completed
```

Codex spells it `thread/started`, `turn/started` / `turn/completed`, `item/started`,
`item/agentMessage/delta`, `item/completed`, over item types `userMessage`, `agentMessage`,
`reasoning`, `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, `todoList`. The Responses
API spells it `response.created` / `response.completed`, `response.output_item.added`,
`response.output_text.delta`, `response.output_item.done`, over `message`, `reasoning`,
`function_call` and the hosted-tool items.

The record uses that shape, and the strongest thing to say about it is that it was not invented
here: two protocols designed independently for streaming reached the same decomposition. One
consequence carries much of this document's weight. **A delta is a first-class thing in both, and is
addressed to a stable item id** — so storing deltas as rows is what these protocols already assume a
consumer does. It is the missing piece rather than a cost.

### Item by item

| Concept                                                                   | Claude Code                                                           | Codex app server                                            | Responses API                                                 | Verdict                                                   |
| ------------------------------------------------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------------------- |
| thread                                                                    | `session_id`, `--resume`                                              | `thread/started`, `threadId`                                | `conversation`, `previous_response_id`                        | universal, and ours                                       |
| turn                                                                      | prompt → `result{subtype}`                                            | `turn/started`, `turn/completed{status}`                    | `response.created` → `.completed`/`.incomplete`/`.failed`     | **universal**                                             |
| several prompts in one turn                                               | folds in mid-turn (measured, <../../cli_protocol/probes/steering.py>) | `turn/steer`                                                | —                                                             | **universal**                                             |
| assistant message                                                         | `assistant` frame                                                     | `agentMessage` item                                         | `message` output item                                         | universal                                                 |
| text delta                                                                | `stream_event`                                                        | `item/agentMessage/delta`                                   | `response.output_text.delta`                                  | universal                                                 |
| item identity                                                             | `msg_…`, absent on many rows, none on deltas                          | `item.id`, `itemId` on every delta                          | `item_id` on every delta                                      | **ours**, with the backend's id as provenance             |
| reasoning                                                                 | `thinking` block inside the message                                   | `reasoning` item, sibling, `{id, summary, content}`         | `reasoning` item; summary streams, content usually encrypted  | **universal, in a changed shape**                         |
| tool call asked                                                           | `tool_use` block                                                      | `item/started` (`mcpToolCall`, `commandExecution`)          | `output_item.added` + `function_call_arguments.delta`/`.done` | universal                                                 |
| tool call answered                                                        | `tool_result` frame                                                   | `item/completed{status, result?, exitCode?}`                | hosted tools only (`mcp_call.completed`/`.failed`)            | universal **at the harness level**                        |
| tool output streaming                                                     | —                                                                     | `item/commandExecution/outputDelta`                         | —                                                             | universal enough to be segments                           |
| per-tool result payload                                                   | content blocks                                                        | `exitCode`, `durationMs`, `result`                          | per-tool                                                      | universal, behind `Json`                                  |
| abort                                                                     | `control_request/interrupt`                                           | `turn/interrupt` → `turn/completed{interrupted}`            | `response.cancel`                                             | universal, **as a turn outcome**                          |
| file change, plan, todo list, web search as kinds                         | these are tools                                                       | first-class items, `turn/diff/updated`, `turn/plan/updated` | hosted-tool events                                            | **rejected**                                              |
| token usage                                                               | `result` usage                                                        | `thread/tokenUsage/updated`                                 | `response.usage`                                              | universal, **deliberately absent**                        |
| prompt                                                                    | `user` frame                                                          | `userMessage` item                                          | client-supplied, never streamed back                          | **ours**                                                  |
| prompt refused, unreadable input                                          | —                                                                     | —                                                           | —                                                             | **ours**                                                  |
| sandbox provisioning, adoption, lease lapse, session end, setup narration | —                                                                     | —                                                           | —                                                             | **ours**                                                  |
| approval asked and decided                                                | `control_request/can_use_tool`                                        | `approval/requested`, `approval/completed`                  | —                                                             | universal at the harness level; not modelled here (below) |

### Reasoning survives, in a changed shape

The vocabulary a second backend is most likely to break on is the one whose current shape is
Claude's, and reasoning is that one. It carries two defects — one a provider's shape promoted
upward, one a distinction never recorded at all:

- **Reasoning is not part of a message.** In Codex and in the Responses API it is its own output
  item, a sibling of the assistant message with its own id. Only Claude nests it, as a `thinking`
  content block. A neutral event carrying a message key therefore asserts a containment two of three
  backends do not have.
- **No backend hands back the raw chain of thought, so the field's name is not the distinction that
  matters.** Anthropic returns _summarised_ thinking, OpenAI returns a generated summary over
  content it keeps encrypted, and Codex streams a summary too. Calling one field `summary` is
  therefore accurate everywhere; what it fails to record is whether anything was disclosed at all,
  so a model that discloses nothing is an empty string with no explanation.

So reasoning is an item like any other, its renderable prose is segments like any other item's, and
its completion carries a **disclosure** discriminator:

| Disclosure | What it means                                                                | Where it comes from                                                                                     |
| ---------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `summary`  | the segments summarise the reasoning; the reasoning itself is not obtainable | Claude `thinking` blocks, Responses `reasoning_summary_text.*`, Codex `item/reasoning/summaryTextDelta` |
| `withheld` | reasoning happened and none of it is disclosed; no segments                  | Claude `redacted_thinking`, Responses `encrypted_content` with no summary requested                     |

That is what makes "the agent thought and you may not see it" renderable. Without it a withheld
reasoning item is an empty string, and no surface can explain the emptiness.

There is no `full`. It would name a disclosure no backend we target performs, so every channel would
have to write a branch it never takes.

### The turn is at this layer

Every backend brackets an exchange, and every backend gives the bracket a terminal status:
`turn/completed{completed | interrupted | failed}`, `response.completed` / `.incomplete` /
`.failed`, `result{subtype}`. The bracket is also **ours** in a second sense — it opens at admission,
before anything crosses a wire, and closes on failures no frame reported — so it is not a copy of
the wire's bracket even where the wire has one. Both readings give the same answer.

**A prompt joining a running turn is a designed operation, not a quirk to bend the schema around.**
Codex exposes it as `turn/steer`, a first-class method. Claude Code does it implicitly, folding a
mid-turn prompt into the running turn at the next tool boundary. That is why prompts stand
many-to-one against turns: two backends both answer several prompts with one exchange, and a record
that could not say so would have to lie about which exchange answered what.

**Deriving "is anything in flight" from an open item instead is the recurring proposal, and it
does not survive.** Codex is itself the first evidence against it: that protocol ships
`thread/status/changed{idle | active}` _alongside_ turns, because "is something happening" and
"which exchange is this" are two questions. Then:

- **A silent turn opens nothing.** An exchange that produces no message and no tool call has no item
  to observe, so the derived answer is "idle" for its whole duration and no surface can report that
  the agent said nothing.
- **An abort needs a subject.** An abort addressed to a session can be accepted when no exchange is
  running. Addressed to a turn it cannot.
- **The outcome has nowhere to live.** All three backends report one status per bracket. Without the
  bracket, `answered` / `aborted` / `failed` is a fact with no row.

What does **not** follow is that a turn contains anything. Items are not nested under turns for
rendering: the transcript is a flat stream of items and a turn is a bracket they name.

Outcomes map as `answered` ← `completed`, `aborted` ← `interrupted`, `failed` ← `failed`. The
Responses API's `incomplete` — output truncated at a token ceiling — folds into `failed`, which is
the one place this vocabulary is coarser than a backend's; a backend adopted for its own sake would
be the reason to widen it.

### What is rejected, and the rule that rejects it

**The line is not how Claude-shaped a thing is, but where the shape lives in the type.** A per-tool
payload behind `Json` is sanctioned — a channel rendering a shell result's `exitCode` knows shell
commands, not Claude. A per-tool shape promoted to a typed member is not.

That rejects Codex's `fileChange`, `todoList`, `webSearch` and `turn/plan/updated` as members of
this vocabulary, even though a real backend emits them. They are one harness promoting particular
tools to items; in Claude Code the same capabilities are the `Edit`, `TodoWrite` and `WebSearch`
tools. A record with a `file_change` kind would be unable to store what a backend without that item
does, and would leave the Claude adapter deciding which tool names deserve promotion. They are tool
calls, and their structure rides in `Json`.

Token usage is rejected on a different ground: it is genuinely universal and has **no reader**.
Universality is necessary and not sufficient. It is named here so that adding it later is a decision
rather than a discovery.

### Where approvals attach

The approval ledger (`mcp_tool_calls`, and the operator/agent authority graph beside it) is a
separate authority with its own lifetime, and is not designed here. It shares no column with any
table below, which is what makes it survive the cutover untouched — and also what leaves an
approval-gated call recorded in the ledger and in no conversation.

The attachment point is one field: a `tool_call` item carries the ledger's `tool_call_id` as an
opaque pointer, with no foreign key, because the ledger's rows outlive the sessions that prompted
them.

## 2. The tables

### The log

`conversation_event` is the record. Every fact is written here, once, and everything else in this
section is either derived from it or belongs to a layer below.

```text
conversation_event
  conversation_id         NOT NULL → conversation
  event_seq               NOT NULL          dense within the conversation, from 1
  PRIMARY KEY (conversation_id, event_seq)
  session_id              NULL    → sessions        ON DELETE CASCADE
  turn_id                 NULL    → conversation_turn
  item_id                 NULL    → conversation_item
  kind                    NOT NULL
  provenance              NOT NULL          'frame_range' | 'authored'
  source_first_frame_seq  NULL
  source_last_frame_seq   NULL
  body                    JSONB NOT NULL
  created_at              NOT NULL
```

Kinds, and the whole set of them:

| Kind                                                                        | Names                                             | Arm      |
| --------------------------------------------------------------------------- | ------------------------------------------------- | -------- |
| `item_started`                                                              | the item, its type, and its type's opening fields | either   |
| `item_segment`                                                              | the item, and a run of its text                   | either   |
| `item_completed`                                                            | the item, and its type's terminal fields          | either   |
| `turn_started`, `turn_ended`                                                | the turn                                          | authored |
| `session_provisioning`, `session_adopted`, `session_ended`, `lease_expired` | the session                                       | authored |
| `setup_narration`                                                           | the session                                       | authored |
| `prompt_rejected`, `unreadable_input`                                       | the conversation, and possibly no session         | authored |

**Prose exists only as segments, and a completion carries none.** This is the invariant the rest of
the design rests on. A backend that streams has its adapter cut the stream into segments; a backend
that produces only a final string — a `result` frame's text, a non-streaming call — has its adapter
emit exactly one segment and then complete. So `item.text` is the concatenation of its segments by
construction, there is no second authority for what an item says, and a consumer replaying from a
position can never reprint prose it already printed.

**The address is dense within one conversation**, which is the property that makes a position
sufficient for a channel. A gap is evidence of loss rather than an artifact of a shared sequence,
"the next one after N" is answerable, and two positions in one conversation can be compared.
`conversation.next_event_seq` is taken under `SELECT … FOR UPDATE` in the writing transaction, which
costs one row lock per write and is affordable: segments are coalesced, so a turn writes tens of
rows and not thousands, and only one session holds a conversation at a time, so the contention is
between that session's fold and prompt admission and nothing else.

The provenance union is unchanged: frames are present on exactly the `frame_range` arm, a range has
two ends or none, and a frame-derived row names both a turn and a session. What is added is that a
frame-derived row must also name its item, so a rebuild can find what the frames produced.

### The materialised entities

`conversation_item` is one row per item, derived entirely from the log.

```text
conversation_item
  item_id          PK
  conversation_id  NOT NULL
  session_id       NULL              absent on a prompt no session has claimed
  turn_id          NULL
  item_type        NOT NULL          'prompt' | 'message' | 'reasoning' | 'tool_call'
  status           NOT NULL          'open' | 'complete' | 'failed'
  opened_seq       NOT NULL          the log position that started it
  closed_seq       NULL              the log position that completed it
  text             NOT NULL          concat of this item's segments, in event_seq order
  backend_item_id  NULL              what the backend called it — provenance, never identity
  origin           JSONB NULL        prompt only: which attachment or surface sent it
  call_id          NULL              tool_call only
  tool_name        NULL              tool_call only
  arguments        JSONB NULL        tool_call only
  outcome          NULL              tool_call only, once closed: succeeded | failed | unknown
  structured       JSONB NULL        tool_call only: the per-tool payload
  disclosure       NULL              reasoning only, once closed: summary | withheld
  created_at, updated_at
```

`status` is the item's lifecycle and nothing else. The overload it replaces put a prompt's queue
state and an answer's completeness in one enum, told apart only by `role`; the queue state now lives
in `conversation_prompt`, where a queue belongs.

Constraints state the per-type fields against `item_type`, `status = 'open'` holds exactly while
`closed_seq` is NULL, and `(conversation_id, call_id)` is unique where a call id is present.

**A tool call's arguments are complete or the call is not started.** Two of three backends stream
arguments as partial JSON (`response.function_call_arguments.delta`, Claude's `input_json_delta`),
so `arguments` is written from the `.done`, and "a call is being composed" is deliberately not
expressible. A channel learns of a call when there is something true to say about it.

`conversation_turn` is one row per exchange, derived from `turn_started` and `turn_ended`.

```text
conversation_turn
  turn_id          PK
  conversation_id  NOT NULL
  session_id       NOT NULL
  first_seq        NOT NULL          log position it opened at
  last_seq         NULL
  first_frame_seq  NULL              bounds into this session's wire log, for appeal
  last_frame_seq   NULL
  started_at       NOT NULL
  ended_at         NULL
  outcome          NULL              answered | aborted | failed
  UNIQUE (conversation_id) WHERE ended_at IS NULL
```

**One open turn per conversation**, not per session: "only one session holds a conversation at a
time" is a conversation-layer rule, so the index that enforces it belongs on the conversation.

The columns a turn no longer carries were the turn loop's own scratch state. Which message a
turn is streaming into is the item of this turn that is still open. Whether it said anything is
whether it has a completed `message` item. Whether a reply is queued is delivery state and belongs
to the channel that owes it.

### The queue

```text
conversation_prompt
  prompt_id             PK
  conversation_id       NOT NULL
  item_id               NOT NULL UNIQUE → conversation_item
  turn_id               NULL           → conversation_turn
  queued_at             NOT NULL
  claimed_at            NULL
  claimed_by_session_id NULL           → sessions
  UNIQUE (conversation_id) WHERE claimed_at IS NULL
  CHECK ((claimed_at IS NULL) = (claimed_by_session_id IS NULL))
```

**Keyed by the conversation, so a prompt may precede a runner.** Admission is a conversation-layer
decision; a session claims a prompt once one exists. A prompt sent to a thread whose sandbox has not
been provisioned is therefore a queued row rather than a refusal nothing can record, and a refusal
that does happen is recordable, because the conversation exists to name even when no session does.

`turn_id` is nullable and many prompts may name one turn, which is `turn/steer` and Claude Code's
mid-turn fold, said once. It replaces a join table: many prompts naming one turn is the same
relation with one fewer row to keep consistent.

The prompt's text is not here. It is the `prompt` item's, so the queue and the transcript cannot
come to disagree about what was asked.

### Channel state

| Table                                                                    | What it is                                                                              |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `chat_attachment`                                                        | one channel holding a copy of a conversation, at its own address. Unchanged.            |
| `channel_cursor(attachment_id, event_seq)`                               | how far this attachment has read the conversation's log                                 |
| `matrix_outbox`                                                          | the Matrix channel's retry queue against its homeserver                                 |
| `matrix_revision(attachment_id, subject, event_id, sent_at, retired_at)` | which homeserver event Matrix is currently editing for a revisable subject              |
| `matrix_sync_watermark`, `matrix_ingress_event`, `matrix_access_token`   | the Matrix channel's own, unchanged but for `matrix_ingress_event` naming a prompt item |

**Only the cursor is channel-generic.** A position in the log is what the conversation layer has to
know about an attachment — it is the resume contract, and the same integer answers it for every
channel. Everything else a channel keeps is its own rendering state, held in its own tables, so a
second channel is a new table rather than a widened shared one.

`matrix_revision` is what `chat_delivery` is once narrowed to what it is read for, and it is
Matrix's: it holds the subjects that channel can **revise** — a status line it edits and retires —
against the homeserver event ids it edits them at. Nothing outside Matrix reads it, and a channel
that cannot edit what it sent has no use for the shape. A row per delivered message is a
flushed-up-to position materialised one row at a time, and the cursor holds that properly.

Keying it by `attachment_id` rather than by a room id is still worth doing, so a channel does not
join its own state through its public address. That the cursor is keyed the same way does not make a
browser tab durable: an attachment row exists only for a channel that holds a copy, so keying by
attachment already excludes tabs.

`matrix_outbox` is `session_outbox` with the session removed. It is keyed by `attachment_id`, holds
`subject` as its idempotence key, and **is written by the channel and never by a turn**: the turn
writes the log and stops. It stays a durable queue because retry state against a flaky homeserver is
real state and a position cannot express "this one failed three times and is backing off".

### Session state, and what this does not touch

`sessions` and `session_frames` keep their shape. The frame log is the record of runner↔console
traffic, addressed by the session that produced it, and its own numbering and vocabulary are a
separate design. `sessions.status` is likewise left alone: replacing it with the timestamps that
actually happened is a session-lifecycle change and bundling it here would double the blast radius
for no shared benefit.

### What is derived from what

| Table                        | What it is          | Key                            | Derived from                                                                 |
| ---------------------------- | ------------------- | ------------------------------ | ---------------------------------------------------------------------------- |
| `conversation`               | identity            | `conversation_id`              | —                                                                            |
| `conversation_event`         | **the log**         | `(conversation_id, event_seq)` | frames, for the `frame_range` arm; the console alone, for the `authored` arm |
| `conversation_item`          | materialised entity | `item_id`                      | the log, wholly                                                              |
| `conversation_turn`          | materialised entity | `turn_id`                      | the log, wholly                                                              |
| `conversation_prompt`        | queue state         | `prompt_id`                    | `queued_at` from the log; the claim is not derivable                         |
| `channel_cursor`             | channel state       | `attachment_id`                | —                                                                            |
| `matrix_revision`            | channel state       | `(attachment_id, subject)`     | —                                                                            |
| `matrix_outbox`              | channel state       | `outbox_id`                    | —                                                                            |
| `sessions`, `session_frames` | session state       | `session_id`                   | —                                                                            |

**A rebuild folds and compares at each level**, and both halves are assertable:

1. Re-fold each session's `session_frames` into events and compare against the log's `frame_range`
   rows. The `authored` rows are preserved rather than re-derived — no frame carries them, so a
   rebuild that replaced everything would silently delete every fact the console alone witnessed.
2. Re-fold the log into items and turns and compare against `conversation_item` and
   `conversation_turn`. This one is total: both tables are derived and neither holds anything the
   log does not.

The invariant worth checking on its own is `item.text = concat(segments)`, because it is what the
whole streaming path depends on and it is cheap to state.

## 3. How a channel resumes

**A channel stores one integer.** `channel_cursor.event_seq` is a position in the conversation's
log, and "I have already shown this" points at that position — never at a message id, never at an
address. A channel that also revises what it showed keeps that in its own table — `matrix_revision`
for Matrix — whose natural subject is an `item_id`.

**A non-revising channel needs nothing else.** What makes a position sufficient are properties of
the log, none of which holds under a schema where prose is a mutated column:

- The sequence is dense within the conversation, so "everything after N" is complete and a gap is
  detectable rather than invisible by construction.
- Every fact is a row. Nothing a channel must show exists only as the current value of a column, so
  there is nothing a position fails to cover.
- Prose is append-only segments, so replaying from a position never reprints what was already
  printed. A resend is at worst a duplicate suffix, which is what an at-least-once channel is
  already prepared for.

**Read, act, then keep.** A cursor is advanced after the channel has done what the events oblige it
to do, so a crash in that window replays rather than skips. A transport with no idempotency key
turns that replay into a genuine duplicate, which is the channel's own problem to declare and mark.

## 4. The cutover

One migration. Existing conversation data is discarded rather than migrated, which is what makes
this affordable and is also the only reason it can be done once.

**It creates the target tables outright; it never alters an old one.** Nothing is carried across, so
there is no `ALTER` to write, and the migration's `upgrade()` is the target schema stated once —
which is also what lets it become the baseline (below) with no second authoring.

### It becomes the baseline, in two steps because a stamped database allows no fewer

The chain is a single baseline plus one revision (`0081`, `0082`), and this should not stack a third
on it indefinitely: the end state is one file that creates the target schema from nothing. Getting
there is two steps, and the reason is worth stating so the second is not read as an oversight.

1. **The cut lands as a revision on `0082`.** Production is stamped there, and a stamped database
   reaches a new schema only by applying something. A file that no-ops for a stamped database
   cannot also transform it, so the transformation is a revision like any other.
2. **Once it has rolled, the three collapse into one.** `0081`, `0082` and the cut merge into a
   single baseline rooted at the cut's own revision — the same move that produced `0081`, now over
   three files instead of seventy, against a production already stamped at the new root.

Step 2 inherits step 1's verification rather than a lighter one: the schema alembic can diff is not
the schema, and `compare_metadata` sees no CHECK constraint, function, trigger, constraint name or
identity sequence. Both steps are checked by building one database through the old path and one
through the new and comparing `pg_dump --schema-only` output — the check that caught a `SERIAL`
where the chain had a `smallint`, and 34 function bodies differing only in indentation.

**Purge live sessions first, while the claim sweep can still reap their sandboxes.** The sweep finds
its work through `sessions`; once the chat tables are gone it cannot, and every sandbox claim is
orphaned. So `DELETE FROM sessions` runs before the migration, not inside it.

**`conversation` and `chat_attachment` rows survive.** Deleting conversations would cascade to every
attachment, and every Matrix room would lose its binding and need re-inviting for nothing. The
threads stay; what hangs beneath them goes.

| Change        | Tables                                                                                                                                                                                                                            |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dropped**   | `session_messages`, `session_prompts`, `session_turns`, `session_turn_prompts`, `session_events`, `session_outbox`, `chat_delivery`, `matrix_room_cursor`, `matrix_ingress_event`                                                 |
| **Created**   | `conversation_event`, `conversation_item`, `conversation_turn`, `conversation_prompt`, `channel_cursor`, `matrix_revision`, `matrix_outbox`, and `matrix_ingress_event` re-pointed at a prompt item                               |
| **Emptied**   | `sessions` and `session_frames` — every row, the tables kept                                                                                                                                                                      |
| **Untouched** | `conversation`, `chat_attachment`, `matrix_sync_watermark`, `matrix_access_token`, the approval ledger, the agent authority graph, the OAuth and operator tables, push subscriptions, node daemons, the recall index's own tables |

### The chat surface is unavailable for the length of the roll

`maxUnavailable: 0` means the previous image serves against the new schema until the roll finishes,
and it selects tables that no longer exist. This is the accepted cost, and it is stated here so that
it is planned for rather than discovered.

**Down for the roll**: the conversation list, the transcript, the follow socket, prompt admission
from the browser, the Matrix sync loop in both directions, any turn a surviving replica is still
running, and the recall index's chat sweep.

**Serving throughout**: the approval queue and the tool-call history — `mcp_tool_calls` shares no
column with any chat table — along with agent enrollment and authority, operator login, the OAuth
connection surfaces and their refresh sweep, Web Push, the node daemons, `/api/capabilities`, and
the frame inspector, which serves an emptied table until new sessions run.

The recall index repairs itself without intervention: its sweep computes the sessions it holds
minus the sessions the source still shows and forgets the difference before indexing anything.

## 5. What changes with it

A map, not a diff.

**Rewritten.** `x/session_store.py` splits along the seams it currently hides — a log writer, a
conversation read, the prompt queue, session lifecycle. `x/session_events.py` becomes the log's one
encoder. `x/conversation_events.py` takes the item shape. `x/claude_code/projection.py` and
`x/frame_projection.py` emit items and segments. `x/session_runtime.py`'s turn loop stops holding
message identity and stops writing the transcript on paths the log does not see.
`x/session_views.py`, `x/transcript_entries.py`, `x/conversation_records.py` and `x/subscription.py`
read items instead of messages. `x/conversation_follow.py` sends appends instead of whole rows.
`x/reprojection.py` becomes the two-fold checker. In the Matrix channel, `outbox.py`,
`room_subscription.py`, `conversation.py` and `sync.py` move onto the attachment-keyed cursor and
the derived outbox. The frontend's conversation page and its generated types follow the read models.

**Deleted.** `SessionStore.begin_assistant`, `update_assistant`, `enqueue_turn_reply` and
`set_message_source_frames` — the independent writers whose absence from the log is what this
redesign exists to end — along with `_enqueue_reply` and the branch by which the conversation's
writer named a channel's address. `delivery_log`'s per-message writes go with the table's narrowing.
The migration tests pinned to dropped tables go with the tables.

**Untouched.** `x/sandbox_claims.py`, `x/session_notifications.py` — the wake carries an id and
nothing else, so it survives a re-keying of everything it wakes — `x/setup_output.py`,
`x/system_prompt.py`, and the Matrix channel's `client.py`, `pacer.py`, `formatted_body.py` and
`ingress_ledger.py`. The approval, agent-authority, OAuth, push and node-daemon halves of the
console are not involved.

**One consumer lives outside the chat runtime and a scoped sweep misses it.**
<../../recall_index/chat_source.py> selects `session_messages` directly. It moves onto
`conversation_item`, and it is the reason that table keeps a stable per-item id, its type, its text
and its timestamps in queryable columns rather than folding them into the log alone.

## 6. What this makes of the work in flight

The chain that began with the conversation-keyed log — the writers supplying `conversation_id`, the
`SET NOT NULL` that follows them, and the separate writers for turn lifecycle, setup narration and
session provisioning — is retargeted rather than abandoned. **The five authored kinds' semantics are
this design's content; only the landing vehicle changes**, from an incremental widening of
`session_events` to the log this document specifies. A reviewer closing those should read them as
merged into the cutover, not as work dropped.

The `SET NOT NULL` step is the one with nothing to carry forward: it re-runs a backfill on a table
that is dropped.
