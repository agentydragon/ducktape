# Chat runtime — what is wrong, and the order to fix it

Three sources feed this: a design review of `haku/console/x/` and its schema, a second review that
read the code de novo once the first's findings had landed, and a live investigation into sessions
that record nothing but a container boot and a death
(<../console/debug/2026_08_13_sessions_boot_and_die.md>). Items are deleted from here as they land,
so everything below is work that has not been done.

Two ambitions shape the ordering, both stated by the owner and neither of them a demand to
generalize now: the runtime should keep a sandbox across a console roll, and it is
single-Claude-CLI, single-Matrix-room by construction where both are eventually wanted plural. They
matter here because several pieces of obvious tidying are the _wrong_ tidying if done without them
in view, which is said at each such point.

## How the stages are ordered

Three constraints still bind, and everything else is preference:

1. **Version negotiation before anything that adds a field to the envelope.** The replay design adds
   one, and now that adoption is live a breaking envelope change no longer means "some sessions fail
   during the rollout" — it means "every live session dies on every console release".
2. **The frontend seam before the backend seam.** The first is smaller, is pure refactoring, and
   does not touch the schema until its last step; the second changes where meaning is extracted.
3. **Contract halves land when their roll converges**, independently of all of this.

Three more are discharged, and are why stages 5 and 6 sit where they do: diagnosis came before
change; a roll is survived before the TTL that was recycling wedged sessions is removed; and a
session survives being asked for before it is allocated on demand. All three rested on stage 1,
which has landed — but stage 1 is verified only by tests, so **the first deliberate console roll
against production is still owed** before anything downstream trusts it
(<../console/debug/2026_08_13_sessions_boot_and_die.md>).

## Stage 2 — make the room behave

_Independent of every other stage; can run in parallel with any of them. Pacing landed: every send
goes through `matrix_pacer.RoomPacer`, and a 429 now reaches it instead of being absorbed inside nio
forever. What is left is the half about meaning rather than rate._

- **Tag what the console sends.** Every question about a room event is currently answered by a
  proxy: msgtype for "is this conversational", sender for "is this ours", nothing at all for "which
  transcript row is this". A namespaced content object naming the session, the transcript row, the
  agent's `msg_…` and a `kind` replaces all three with statements — and makes delivery idempotent
  for free, which is the ledger stage 4 would otherwise have to invent. Public and federated, so ids
  and kinds only; stripped by redaction; absent on existing history, so today's msgtype rule stays
  as the fallback.

**Done when** every event the console sends says what it is.

## Stage 3 — version the bridge so it can evolve

_Before stage 4, which adds a field to the envelope._

`PROTOCOL_VERSION` is 2 and rides on `start`, typed `Literal[2]`. Three properties are wrong for a
world where the runner outlives many console releases: it flows from the end that cannot adapt to
the end that must (the console never learns the runner's version); exact match cannot negotiate; and
`extra="forbid"` makes every additive field a fleet-wide break.

- **Evolve by adding kinds, not fields.** An unknown `kind` already fails the union parse —
  fail-closed exactly where a must-understand change belongs — while an optional field a peer
  ignores leaves it behaving as its version correctly did. So: unknown kind rejects, unknown field
  is ignored, must-understand changes arrive as new kinds.
- **A supported range, not a number**, which only stays cheap given the above: the console emits
  frames as well as parsing them, so with `forbid` on the far end a range means one serializer per
  version. The range and the field policy are one decision.
- **The runner speaks first.** Negotiation needs a fixed point, and today the version rides on the
  console's first frame — so the console must choose before hearing anything, and the runner cannot
  state its range until it has decoded a frame whose shape is what is in question. A minimal `hello`
  carrying only the supported range, its shape then **frozen forever**, with the console replying
  `start` or `resume` in the chosen version.
- **A deletable transition shim.** A v2 runner waits for `start` and rejects unknown kinds, so the
  console waits briefly for a `hello` and on silence sends the v2 `start` the peer expects. One more
  flag day buys the end of flag days; write the deletion condition into the tombstone.
- **Contract tests at both ends of the range.** The repo runs this discipline for FastMCP as an
  exact pin; a range inverts it, or "we support 2" becomes a claim nobody checks.

**Done when** an old runner image and a current console interoperate in a test, and adding a field
to the envelope breaks nothing.

## Stage 4 — survive a roll mid-turn

_Needs stage 3. This is where the queues earn their place._

- **A bounded outbound buffer in the runner**, re-sent on adopt.
- **Replay is safe because frames carry identity, not because the cursor is exact.** The console can
  die between recording a frame and acknowledging it, so a frame is replayed however exact the
  cursor was — which makes the cursor an optimisation and identity the correctness argument. The
  frames the console keeps already carry agent-assigned identity: `assistant` → `message.id`, `user`
  → the `tool_result`'s `tool_use_id`, `result` → its turn, `command_lifecycle` →
  `(command_uuid, state)`, `system/task_*` → `task_id`.
- **Except deltas, which is the one class replay corrupts.** A `stream_event` has no identity and
  `streamed += delta` double-appends. It is also the class that never needs replaying, and the
  recorder already drops them — so "buffer everything except deltas" is a rule that already exists
  for another reason.
- **Dedupe at ingestion, not at storage**, since a replayed `assistant` frame that reaches
  `_run_turn` posts to the room a second time. A nullable `frame_uid` with a partial unique index on
  `(session_id, frame_uid)` makes it a schema property; deriving Matrix's `txn_id` from the message
  id (instead of `uuid4().hex`) makes the homeserver a second line of defence.
- **Close the claimed-but-never-delivered window.** `next_prompt` claims the prompt and opens the
  turn in one transaction; `_run_turn` writes it to the CLI afterwards. A replica dying in between
  leaves a claimed prompt never asked and a turn that never ends — harmless while the session dies
  with it, real once sessions survive. `command_lifecycle`'s `queued`/`started` is what distinguishes
  "delivered, answer coming" from "never left".
- **Route an adopted turn by turn**, not by session.

**Done when** a roll during a turn loses no answer and posts nothing twice.

## Stage 5 — let the console own the lifecycle

_Needs stages 1 and 4 (constraint 2)._

`shutdownTime` is set to `now + session_ttl_seconds` at claim creation and **never patched**, so it
is not an idle timeout: a conversation in full flow dies at exactly two hours, mid-turn, and the room
is told it failed. Removing it is cheaper than it looks, because the TTL is not what prevents leaks —
the Kyverno `CleanupPolicy` reaps Sandboxes and SandboxClaims older than 24h at the CR layer, which
is the real backstop.

- Drop `shutdownTime` to the janitor's horizon or omit it; release on an idle timer instead, with the
  lease as liveness and the janitor as what catches a console that forgot both.
- **Keep a horizon, because it is the protocol-compatibility window.** A runner's image is fixed at
  claim creation, so the oldest live runner is exactly as old as the longest-lived session — which is
  exactly how far back the console must still speak the bridge protocol. Pick it deliberately and
  derive the support range from it.
- **An expired lease should mean unowned, not dead.** `expire_stale_leases` fails the session and
  provisions a replacement; re-adoption wants adoptable, which could not land before an adopter
  existed and now can.

**Done when** no session dies on a clock, and an idle sandbox is released rather than reaped.

## Stage 6 — allocate a sandbox because there is something to do

_Needs stages 1 and 5 (constraint 3)._

An idle room holds a sandbox permanently: the supervisor provisions whenever the room has no live
session, the warm pool is `replicas: 0` so every claim is a cold start, and the cycle repeats on the
TTL — twelve cold starts a day for a room nobody speaks in, each announcing itself and narrating its
bootstrap there, holding ~1 CPU / 2Gi of an 8 CPU / 16Gi quota in between.

The SPA has a gesture that means "I want a session" and Matrix has none, so the supervisor
substitutes by assuming demand permanently. The prompt is the honest substitute, and the prompt
queue already made it cheap: accepted and running were separated when `claude_chat_prompts` landed.

- `create()` writes the row and stops, in a new **`idle`** status; `allocate()` mints the credential
  and the claim and moves to `provisioning`.
- Admission accepts on `idle`, so `enqueue_prompt` is what creates demand; the supervisor's trigger
  becomes "an unclaimed prompt and no sandbox", waking on `ChatEventKind.PROMPT`.
- `MatrixTurns.offer` stops refusing an unallocated session, so the batch enters the durable queue
  rather than being left on the homeserver.
- **`LIVE_SESSION_STATUSES` currently means both "worth keeping" and "has a lease to renew".** An
  idle session is the first that is worth keeping with no holder to lose; split the set rather than
  giving it a fake far-future lease.
- **Adding an enum value is two releases here**, not additive: `TextBackedStrEnumColumn` parses the
  column, so a replica on the previous image reading `idle` fails rather than degrading. Widen the
  member and the CHECK first; write it next release.

**Cost, stated plainly:** the first message after quiet pays the full cold start. Measure it rather
than assume it away.

**Done when** an idle room holds no sandbox and the first message provisions one.

## Stage 7 — the two seams

_Everything above is possible without these; they are what stop the next feature from being a
rewrite._

### The frontend seam — first, and mostly refactoring

`RoomSurface` takes `room_id` on all six methods and `MatrixSurface` opens five of them with
`del room_id`, because its channel is already bound to the one room. So the port reads as multi-room
and behaves as one-room-globally, and `room_id: str | None` threads through the turn loop making four
call sites re-ask a question answered once per connection — plus three no-op coroutines so
`_TurnStatus` has something to call.

1. A `ChatFrontend` port with **no address parameter**: a null implementation for the SPA, which
   needs none of it because its client reads the message rows, and `MatrixSurface` bound at
   construction as it effectively already is. Every `or room_id is None` and all three no-ops go.
2. Then the schema half — `chat_attachment(session_id, surface, address, attached_at, detached_at)`
   with a partial unique index on `(surface, address) where detached_at is null`. It subsumes
   `claude_chat_sessions.surface`, `.room_id`, both check constraints tying them together, and
   `matrix_conversation.session_id`; the pointer/history distinction those two tables document in
   prose becomes `detached_at IS NULL`. Attach/detach within one session becomes a row.

### The backend seam

Launch and transport are already a package boundary, the transcript's shape is already neutral, and
the frontends are a different question. What is welded is **meaning**: `_run_turn` matches on the
CLI's own frame `type` and six helpers parse Anthropic content blocks, so the turn loop is a frame
interpreter. Every branch of it reduces to five events naming nothing provider-specific —
`TextDelta`, `MessageCompleted(id, text, calls)`, `ToolResult`, `Activity`,
`TurnCompleted(outcome, text, cost, usage, duration)` — with one adapter per backend producing them.
`ClaudeCli.frames()` already sits where that adapter goes; it yields wire dicts, which is why the
console became the interpreter.

Two corrections this forces on items that would otherwise look like cleanups:

- **`tool_uses` must be deleted into a `tool_call` table**, written by the adapter — not into a parse
  of Claude frames, which only sessions on that backend have. It answers the lossy-copy objection by
  holding the result, and it is where the rollout join lands anyway.
- **"Projection or primary" is settled against the projection.** The rollout is one protocol's wire
  and cannot be the record a second backend shares. Transcript primary, rollout per-backend evidence
  — so what to remove from the duplication is the double write and `agent_message_id`.

**Done when** a second frontend and a second backend are representable without touching the turn
loop's signatures.

## Anytime — independent of the stages

- **Every stream delta re-reads the whole session.** `update_assistant` NOTIFYs per delta →
  `_sse_stream` wakes → `store.get` → `_rollout_calls` selects every `assistant`/`user` frame and
  re-parses its content blocks. O(session) per token batch, paid only while the SPA is streaming.
  Fix by indexing incrementally on the agent message id, or by scoping the read to the messages
  asked about.
- **Split `claude_chat.py`** (~2000 lines: view models, the Kubernetes claim client, the store, the
  recorder, the status driver, the service, the turn loop, the port, the routes). The ~230 lines of
  `KubernetesSandboxClaims` and its five dict-poking helpers have nothing to do with chat.
  `ClaudeChatStore`'s twenty-odd methods split along the seams the turn table and the prompt queue
  already created — each split landing with the change that creates it, never as a standalone
  reshuffle with no acceptance criterion.
- **Prune the archaeology.** Roughly 150 of `claude_chat.py`'s lines narrate what the code used to be
  and which bug that caused; STYLE puts those under Remove. Keep the invariant as one imperative
  line ("do not gate on session status: admission asks the turn"), move the story to `debug/`.
- **`ClaudeChatFrame.kind` is filtered with `ChatMessageRole`** — different domains sharing two
  spellings, which breaks the moment either gains a value. It wants its own `StrEnum` beside
  `FrameDirection`.
- **`bridge_token_fingerprint = b""`** is a cleanup-pending tri-state on a credential column, and the
  zero value STYLE says never to use for absence. It wants to be `claim_cleaned_at`.
- Smaller: `list_turns`/`read_frames` take `str` session ids and parse them, where the boundary is
  the MCP tool; the store imports its read models from the MCP tool that reads it;
  `_message_view`'s `_NO_CALLS` default serves one caller that structurally cannot need it;
  `KubernetesSandboxClaims._clients()` is double-checked locking with four asserts;
  `MatrixTurns.offer` pre-checks the status that `enqueue_prompt` then checks atomically, and its own
  comment admits it races; `claude_chat_turn_prompts` has no reader until folding lands; "surface"
  names five things and "turn" three.

## When their rolls converge

- **The prompt queue's compatibility half.** The transcript row is still minted `pending` and the
  `_legacy_pending` scan still answers a prompt an old replica accepted. Both tombstoned. Write the
  row final and drop the scan; `'pending'` stays in the CHECK constraint.
- **`tool_uses`.** Server default first (`SET DEFAULT '[]'::jsonb`), then stop writing it, then
  `drop_column` in a third release — an old replica's `_message_view` selects the mapped column by
  name. Into the `tool_call` table of stage 7, not into a frame parse.

## Later

- **Mid-turn steering.** Measured to work: a prompt written while a turn runs is absorbed at the next
  tool boundary and one `result` covers both. `claude_chat_turn_prompts` is many-to-one already and
  admission deliberately still refuses, with a test saying so. Needs `interrupt` with
  `cancel_queued: true`, since a bare interrupt starts the next queued prompt.
- **Streaming the answer into the room.** Achievable only as a coarse refresh — the five-second floor
  is Synapse's rate limit and what a person can read, and every edit is a permanent federated event.
  Stream into the status line's mechanism (a notice, edited, redacted at the end) rather than editing
  the reply, because editing the reply puts partial drafts into the tail `recent_messages` reads and
  the msgtype trick that excludes chatter does not apply to an `m.text` edit.
- **Source the CLI from npm** rather than out of the Agent SDK wheel
  (<cli_protocol_ownership.md>).
