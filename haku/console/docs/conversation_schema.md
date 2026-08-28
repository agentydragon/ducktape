# The conversation record

What a thread is, as tables: the log every fact is written to, the entities materialised from it,
and the state each channel keeps beside it.

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
conversation table, which is what makes it survive the cutover untouched — and also what leaves an
approval-gated call recorded in the ledger and in no conversation.

The attachment point is one field: a `tool_call` item carries the ledger's `tool_call_id` as an
opaque pointer, with no foreign key, because the ledger's rows outlive the sessions that prompted
them.

## 2. The tables

The schema itself lives in <../database_schema.py> — `Conversation`, `ConversationEventRow`,
`ConversationItem`, `ConversationTurn`, `ConversationPrompt`, `ChannelAttachmentRow`, `ChannelCursor`,
`Session`, `SessionFrame`, and the Matrix channel's own tables — with every column, constraint and
index documented where it is declared. The stored event kinds and their provenance arms are
`ConversationEventKind` and `AuthoredEventKind`, and the body shapes the log stores, are the one
vocabulary in <../conversation/conversation_event.py>. `conversation_event` is the record: every
fact is written there, once, and everything else is either derived from it or belongs to a layer
below. What no single model can state is which table derives from which, and the invariants that
span them:

- **`conversation.runtime_kind` names the adapter that owns the session wire; the event vocabulary
  stays provider-neutral.** The pin is the conversation's, so a replacement session inherits it.
- **The prompt's text is only the `prompt` item's**, so the queue and the transcript cannot come to
  disagree about what was asked.
- **One open turn per conversation, not per session.** "Only one session holds a conversation at a
  time" is a conversation-layer rule, so the index enforcing it (`uq_conversation_turn_open`) keys
  on the conversation.
- **Only the cursor is channel-generic.** A position in the log is the resume contract, and the
  same integer answers it for every channel. Everything else a channel keeps is its own rendering
  state in its own tables — **written by the channel and never by a turn**: the turn writes the log
  and stops. A second channel is a new table rather than a widened shared one.
- **`sessions` and `session_frames` are the session layer's own design** — the wire log's numbering
  and vocabulary are <harness_frame_log_v3.md>'s, and a session's `status` is derived from stored
  facts at read time (`database_schema.Session.status`).

### What is derived from what

| Table                                                        | What it is          | Derived from                                                                 |
| ------------------------------------------------------------ | ------------------- | ---------------------------------------------------------------------------- |
| `conversation`                                               | identity            | —                                                                            |
| `conversation_event`                                         | **the log**         | frames, for the `frame_range` arm; the console alone, for the `authored` arm |
| `conversation_item`                                          | materialised entity | the log, wholly                                                              |
| `conversation_turn`                                          | materialised entity | the log, wholly                                                              |
| `conversation_prompt`                                        | queue state         | `queued_at` from the log; the claim is not derivable                         |
| `channel_attachment`, `channel_cursor`, the Matrix channel's | channel state       | —                                                                            |
| `sessions`, `session_frames`                                 | session state       | —                                                                            |

**A rebuild folds and compares at each level**, and both halves are assertable:

1. Re-fold each session's `session_frames` into events and compare against the log's `frame_range`
   rows. The `authored` rows are preserved rather than re-derived — no frame carries them, so a
   rebuild that replaced everything would silently delete every fact the console alone witnessed.
2. Re-fold the log into items and turns and compare against `conversation_item` and
   `conversation_turn`. This one is total: both tables are derived and neither holds anything the
   log does not.

The invariant worth checking on its own is `item.text = concat(segments)`, because it is what the
whole streaming path depends on and it is cheap to state.

**Reads hand out what the fold materialised, and the agent-facing item read is one of them.**
`haku_conversations.read_conversation_items` pages `conversation_item` and `conversation_turn` by their
defining stream positions; it re-reads neither `session_frames` through an adapter nor the log
through a second fold. So the reads cannot disagree with the items and turns the writer derived,
a reader of them needs neither a session's `runtime_kind` nor any harness vocabulary, a
conversation that ran before a projection fix keeps the history it had rather than acquiring a new
one on the next read — and a page's cost is the page's rows, not the conversation's length. What
such a reader can fail to read is therefore a value in the schema it owns — an `item_type` a newer
release minted — which the strict decode refuses loudly rather than skipping.

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
