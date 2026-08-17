# One state machine instead of two

The chat runtime handles a lot of corner cases, and they nearly all sit in one place. This is what
that place is and how to remove it. It supersedes nothing in
<chat_runtime_cleanup.md> — that plan's remaining stages are about behaviour, this one is about
where the behaviour's state lives.

## The problem, stated once

`_run_turn` holds a turn's state in **local variables** — `assistant_id`, `streamed`, `spoke`,
`saw_assistant_message`, `result`. Everything durable is a side effect of them.

So when the process holding those locals dies, a second body of code exists to reconstruct them
from the log: `adopt_open_turn` and its helpers `_prompt_left`, `_recorded_result`,
`_said_anything`, `_streaming_assistant`, `_requeue`. That is the same state machine written
twice, and every corner case the runtime has needed is at the seam where the two disagree:

| Symptom                                            | Which disagreement                                                                    |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- |
| A claimed prompt nobody ever asked                 | The live path claims before writing; recovery has to guess whether the write happened |
| A turn whose `result` is logged but never closed   | Recovery has to detect a death between two writes                                     |
| `spoke` cannot tell delivered from merely recorded | Recovery cannot rebuild a local that was never durable                                |
| `TurnResumed.streamed`                             | Recovery re-seeding a local by hand                                                   |

The last two are closed: stage 3 below made that state durable, so `_said_anything`,
`_streaming_assistant` and `TurnResumed`'s state fields are gone. The first two are still asked of
the frames, and stage 4 is what retires them.

Roughly fifty comment sites across `session_runtime.py` and `session_store.py` reason about
"already / twice / replay / duplicate". That is the tax, and the store split (#4146) moved it
rather than paying it.

## The shape

Two stages with a **durable cursor** between them.

**1. Transport ↔ log.** The socket's only job: CLI frames into `session_frames`, deduplicated
by `frame_uid`; queued prompts out to the CLI. It knows nothing about turns, messages or rooms, so
it can die anywhere without leaving a decision half-made.

**2. Log ↔ world.** A fold, `project(state, frame) -> (state, effects)`, reading from a per-session
cursor. Effects are rows: message upserts, turn open and close, room-outbox entries. **The cursor
advances in the same transaction as its effects**, which is what makes them exactly-once.

The payoff is not the two loops. It is that **live and recovery become the same code path**:
steady state is "project each frame as it lands", adoption is "project from the stored cursor,
which happens to be behind". There is no second implementation left to disagree with the first.
It costs no latency either — the fold runs inline in the happy path. Two loops logically, one call
stack in practice.

**This paragraph is the specification, the first implementation of it diverged, and the code has
since moved back.** `claude_code/projection.py` shipped as `project(frames) -> Projection` — stateless
over a whole frame sequence, with the cross-frame state private to the call and discarded when it
returned. That shape can be re-run over a session but cannot be _resumed_ from a cursor, which is
the property this paragraph exists for. It is now `project(state, frames) -> (state, Projection)`
over a neutral `ProjectionState`, with the end of the stream an explicit `finish(state)` rather
than a batch running out, and one batch equal to any split of batches asserted as a test.

**The cursor is `sessions.projected_frame_seq` (migration `0050`)**, advanced by
`SessionStore.apply_frame` in the same transaction as the frame's effects, and adoption hands the
turn loop the frames past it rather than asking the log a second time what they meant. The
question the shape left open — a reducer's cursor is only resumable if the state at that cursor is
recoverable — is answered by the loop's own granularity: it seeds a fresh state per frame, so the
state at every cursor position is the empty one. The moment that changes, `first_frame_seq` bounds
the re-projection to one turn, which is measured to cost about a millisecond per thousand frames
(the heaviest session in <../console/debug/frame_shape_census.md> is 14k frames, deltas included).

What that deletes: `TurnResumed`, `adopt_open_turn`'s three-way case analysis, `_recorded_result`,
`_said_anything`, `_streaming_assistant`, `_prompt_left`, `saw_assistant_message`. `spoke` stops
being a guess and becomes "the cursor passed this frame". Stage 3 took the first, the third, the
fourth and the last of those; the cursor took the case analysis and left `_recorded_result` behind
a tombstone for sessions that predate it. `_prompt_left` stays and is not the cursor's to take: it
asks whether the console's own outbound write happened, and an outbound prompt projects to nothing
by design.

Room delivery becomes an outbox drained by `channels/matrix/pacer.py`, which is already a queue and needs only
to become durable. Then `spoke` is "the outbox row is marked sent", `EventTag.transaction_id` is
uniformly the outbox row id with no derive-versus-mint rule left, and `_deliver_reply`'s
"deliberately not fatal, TODO retry" resolves itself — retrying is what an outbox does.

## What this is not

**Not a large line-count win.** Estimate: −350 from the reconstruction cluster and the partial-frame
machinery, +150 for the fold and the outbox, against a 2400-line file. The win is that the number of
things which can disagree goes from two to one.

**Not a big-bang rewrite.** It is the hot path in production, and the stages below are ordered so
each is independently useful and reversible.

## Stages

Stage 1 — a log with no hole for the fold to trip over — is the foundation the rest needs and is in
place: every frame class is recorded, deltas included, where before the delta gap was precisely what
forced `streamed` to be carried by hand.

The one thing it left owed — **measure the delta cost** before stage 2 commits to a row per delta —
was paid by the production census (#4114, <../console/debug/frame_shape_census.md>): deltas occur in
a minority of sessions, are heavy where they occur, and the row-count increase they impose on the
heaviest session is stated there. Stage 2 can proceed on that number rather than on an estimate.
`read_frames` already keeps deltas out of its default view, and the extra write per delta is a wash
once stage 2 removes the `partial` row it replaces.

### 2. Make the frame log store one thing — recorded, and numbered by the wire

Two changes, and they are deliberately one stage: the table stops holding two vocabularies in
`kind`, and it stops taking its ordering from Postgres. They share a cause — the sink sits _above_
the envelope and structurally cannot see one — and therefore share a fix, which is moving that sink
down onto the socket. Sequencing them apart would move it twice.

#### 2a. `kind` holds two vocabularies

`session_frames.kind` holds **two different discriminator vocabularies**, because two unrelated
sinks write to it:

| Writer                                                  | Sees                                      | Writes into `kind`                             |
| ------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------- |
| `RolloutRecorder._record`, a `FrameSink` on `ClaudeCli` | CLI protocol frames only, by construction | `payload["type"]` — the CLI's vocabulary       |
| `_progress_reporter.report`, by hand                    | one decoded line of a `SetupOutput`       | `"setup_output"` — the bridge's `kind` literal |

Plus a `partial` row, the console's own reconstruction of a streaming answer, which wears
`assistant` and is told apart by a boolean column. That one leaves regardless: it is tombstoned on
`update_partial_frame`, and stage 1 removed its reason to exist.

**The intended shape, if and when this is picked up: the table is the log of the bridge.** Nothing
here is scheduled, and the rest of this plan does not depend on it — stage 3 onward can proceed with
the schema exactly as it is. `kind` becomes the envelope discriminator
(`claude`, `setup_output`, `hello`, `start`, `end_input` — `protocol.py` owns the list) and the
CLI's own `type` gets a column of its own. Two columns, each answering one question, and any
future runner-originated frame has a home instead of a special case.

**What this costs, stated before starting.** The recorder is a `FrameSink` on `ClaudeCli`, which
sits _above_ the envelope and structurally cannot see one — so the sink moves down to
`WebSocketTransport`. The dedup answer moves with it: `received()` returns False for a replay and
`ClaudeCli._read` uses that to skip routing, so the transport takes over both. It also means
recording envelope kinds nothing reads today (`hello`, `start`, `end_input`); that is the point of
the design rather than a cost, but it is new rows.

**No dedup win to claim.** `SetupOutput` is queued `replayable=False`, and `prepare_workspace`
sends it straight to the socket before launch anyway — so it is never replayed and there is no
duplicate-row bug here for A to fix.

#### 2b. The number is the runner's, not Postgres's

**Decided, not open** (operator, 2026-08-16): _"I do think we really should do the runner owned
numbering and use it for existing offsets, it also makes catch up trivial."_ So `frame_seq` is
minted on the wire and becomes the log's own offset, rather than a second column beside a
database-assigned one.

Today it is `BigInteger, Identity(always=True), primary_key=True`, travelling back to the client on
`RecordedFrame.frame_seq` after the `INSERT`.

**The motivating property is catch-up, and nothing else gives it.** A dense counter the runner owns
turns reconnect into _"send me everything after N"_: the runner answers from its own retained
window with no database round trip and no reconciliation. An `Identity` cannot be asked that. It is
sparse, so a hole in it is not evidence of anything; it is unknowable until after the write; and it
is the console's fact about a row rather than the wire's fact about a frame. Today's adoption
therefore re-sends the **whole** window every time and leans on `frame_uid` to sort it out — which
works for the classes that carry an agent-assigned id and cannot work for the ones that do not.

Three secondary properties come with it and are worth keeping in view. The number exists the moment
the frame is read off the socket. It is true arrival order rather than insert order. And
`ReceivedFrame.frame_seq: int | None` becomes structurally impossible rather than avoided by
convention — #4164 deleted the `_UNNUMBERED_FRAME` sentinel, and this is what removes the seam that
made one necessary.

##### Where the counter lives — a correction to what 2a implies

2a says the sink moves to `WebSocketTransport`, and it is tempting to read that as "so the counter
lives there too". **It does not, and the reason is the whole design.** `WebSocketTransport` is the
_console_ side of the bridge socket (`haku/runtime/x/bridge/transport.py`; it sends `start`, reads
the runner's `Hello`). The console is the end that is replaced mid-conversation — that is what a
roll is — while the runner process outlives every socket it serves, by construction (`runner.run`
holds the CLI across reconnects). A cursor a reconnecting console hands back has to be a number its
_peer_ minted, or the peer cannot act on it.

So: **`runner.py` mints, at the point a frame is put on the wire, and stamps it on the envelope.**
`WebSocketTransport` is where the number is _read and recorded_, which is the job 2a moves there.
Two different verbs on the two ends of one socket.

Numbering at send rather than at read also fixes it once: a frame retained in the replay window
keeps the number it went out under, so the console that adopts the session and the console that
died see the same integer for the same frame.

##### Seeding across reconnect

A runner's counter is per **process**, which is per sandbox, which is per session — so in the happy
case it needs no seeding at all. What needs seeding is the case the design exists for: the console
must be able to say where it has got to, and two consoles may be replaying one runner's window at
once during a roll. So the cursor must be **per session, not per connection**, and it must be a fact
about the log rather than about a socket.

`ClaudeLaunch` (`start`) carries it: `resume_from`, computed by the console as the highest
runner-minted sequence recorded **for that session**. `start` is sent on every connection — the
runner ignores its launch content on a reconnect but does read the frame — so this needs no new
frame and no new round trip. The runner takes `next = max(next, resume_from + 1)` and replays only
its retained frames above `resume_from`.

Two consoles racing therefore compute the same cursor from the same rows, which is the property that
makes this per session. And a runner whose process really did restart is seeded back above what the
console holds rather than colliding with it from 1.

**Why this rides as an added field and not a `PROTOCOL_VERSION` bump.** `SUPPORTED_VERSIONS` is a
single element, so a bump does not negotiate — it _refuses_ the peer on the other number. A runner's
image is fixed when its SandboxClaim is created and a live session outlasts console releases for as
long as a replica tends it (`_renew_lease` slides the claim's `shutdownTime` on every heartbeat, so
nothing bounds that), so a bump kills every session in flight on the release that ships it.
`protocol.py` already made the opposite trade for exactly this reason: `extra="ignore"`, so an
unknown _field_ is dropped by a peer that predates it while an unknown _kind_ still fails closed.
`seq` and `resume_from` are therefore optional fields on frames that
already exist, and both directions of skew degrade to today's behaviour: an old console sends no
`resume_from` and gets the whole window; an old runner sends no `seq` and the console records none.

##### What "dense" buys, and what enforces it

Dense means consecutive frames from one runner differ by exactly one, so a hole is evidence rather
than noise. Two checks fall out, and they are different questions:

- **Live contiguity.** Within one connection, the transport compares each frame's `seq` against the
  last. A hole means the socket delivered out of order or the runner's buffer overflowed — neither
  should be possible, so it is a bug report, not a recoverable state.
- **Resume completeness.** On adoption, the runner replays from `resume_from`. If the oldest frame
  it can still offer is above `resume_from + 1`, its window has rolled past what the console
  recorded and those frames are gone for good. That is the case today's design cannot even see.

**What a consumer does with a gap.** Not "carry on quietly", which is what happens now. The
projection over a gapped log is not trustworthy — a message can be missing the frame that closed
it — so the gap is recorded as a session event (§ stage 4's second category has the home for it) and
surfaced in the frame inspector, which is the surface an operator already opens to appeal a
transcript. Escalating further (failing the turn) is deliberately not proposed: a lost `stream_event`
is cosmetic while a lost `result` is not, and the log cannot tell which is missing.

**Neither check can be written before the cutover, and it is worth saying why rather than
rediscovering it.** Both read a run of numbers and ask whether it is dense, and neither run is dense
yet:

- The console's recorded numbers are not. A `SetupOutput` is numbered by the runner like everything
  else, but it reaches the log through the progress reporter — where one frame decodes into however
  many complete lines it finished — so its number is on no row. Every bootstrap therefore leaves a
  hole that means nothing.
- The wire's numbers are not either, on the connection that matters. The replay window retains only
  `replayable` frames, so an adopted connection is handed a sequence with a hole wherever a delta
  or a narration line was, and the console cannot tell where the replay stopped and the live
  stream began.

R2 answers both: it is the release that gives every runner frame one row, and it puts the transport
at the end holding both the cursor it sent and the frames coming back. Adding a check before then
reports on narration and on the replay window's own design, which is a warning nobody can act on —
the fastest way to teach an operator to ignore it.

**One thing dense numbering buys that `frame_uid` never could.** `frame_identity.py` argues, and it
is right, that a `stream_event` must not be replayed: it has no agent-assigned id, and
`streamed += delta` double-appends. A dense sequence is an identity for the frames that have none —
not of their _content_, which is what the module correctly refuses to invent, but of their
_position_. Once the console dedupes on `(session_id, frame_seq)` rather than on `frame_uid`, a
replayed delta is refused by the key before it can reach the loop, and the "never replay a delta"
rule is a bound on the window's size instead of a correctness argument. That is also the point at
which `REPLAY_WINDOW = 500` has to be re-sized or given a byte budget, because a delta-heavy turn
runs to thousands of frames.

##### The primary-key question

`frame_seq` is the primary key, and it is read by `FrameCursor`, both keyset reads
(`read_frames` forwards, `read_operator_frames` backwards), `session_turns.{first,last}_frame_seq`,
`session_messages.source_{first,last}_frame_seq`, the `haku_conversations` MCP tools, and the frames
page. Client-supplied values mean dropping `Identity` and enforcing uniqueness per session instead of
globally.

**The constraint that shapes it: there are two minters into one space, and there is no way around
that.** The runner numbers what crosses the socket, but the console writes rows the runner never
sees — a console→CLI write is recorded before the runner has it (`RolloutRecorder.sent`, whose
return is the operator prompt's `source_first_frame_seq` via `set_message_source_frames`), and a
console-origin session event (an ownership change) crosses no wire at all. Two independent minters
cannot share one dense integer space: either they **partition** it or the key carries a
**tiebreak**. Partitioning by a reserved stride was considered and rejected — the band between two
runner frames is bounded by hope, and an overflow is a primary-key collision on the hot path. So:

- **`frame_ord SMALLINT NOT NULL DEFAULT 0`**, and the primary key becomes
  `(session_id, frame_seq, frame_ord)`. A runner frame is `frame_ord = 0`. A console-origin row
  recorded while the console's high-water mark is _N_ takes `(N, k)` for the next free _k_, so it
  sorts strictly after runner frame _N_ and strictly before _N+1_ — the same fidelity insert order
  gives today, stated rather than implied.
- **Every cursor still names one integer.** `FrameCursor`, `before_seq`, the MCP tool arguments and
  the frontend are unchanged: a cursor of _N_ is `(N, 0)` in the composite. Only the `ORDER BY` and
  the key change. `session_turns.first_frame_seq` is already documented as a _bound_ rather than a
  pointer, so it is unaffected in kind.
- **`session_messages.source_*` stays two integers**, and an inclusive range over a composite order
  is still well defined — the range is over positions, and a position is a `frame_seq`.

**No session may hold both, and the fault is loss rather than untidiness.** Identity values are
global and run far above 1; runner values are per session and start at 1. Uniqueness and ordering
are per session, so the two never have to be comparable — but a session carrying both breaks
catch-up outright: `resume_from` is that session's `max(frame_seq)`, and a cursor in the tens of
thousands selects nothing from a runner window numbered from 1, so the reconnect replays nothing and
whatever the dead replica missed is gone. The sessions that can hold both are the ones recorded
before the cutover, and they are deleted rather than adapted (§ _The old rows are purged, not
carried_).

**Dropping `Identity` is the one step that is not additive**, and the trick that makes it roll-safe
is that it does not have to be a drop. `ALTER COLUMN frame_seq DROP IDENTITY` followed by
`SET DEFAULT nextval(...)` on a sequence seeded above the current maximum leaves an old replica —
which inserts without naming the column — behaving exactly as before, while a new replica may supply
its own value. `GENERATED ALWAYS` is what refuses a supplied value; a plain default does not.

##### Catch-up, end to end

1. The console's replica goes away mid-turn. The runner's socket drops; its CLI keeps running and
   its pipes keep draining into the outbound buffer (`_drain_cli` is long-lived on purpose).
2. A new replica admits the redialing runner (`authenticate_bridge`), and before sending `start`
   computes `resume_from = max(frame_seq) WHERE session_id = … AND frame_ord = 0` — one indexed
   lookup on a key that already exists.
3. The runner seeds its counter above that and re-sends every retained frame with `seq > resume_from`,
   then continues live from where it left off. Frames the dead console _did_ record are never
   re-sent, which is the round trip today's design pays on every adoption.
4. The console records each arrival with `ON CONFLICT (session_id, frame_seq, frame_ord) DO NOTHING`.
   **Exactly-once falls out of that**, and out of nothing else: the key is the frame's position, so a
   frame offered twice inserts once whether or not the CLI gave it an id, and `fresh` (which is what
   stops the turn loop acting on a replay) is still just "the insert happened".
5. If the runner's oldest retainable frame is above `resume_from + 1`, the window rolled past what
   was recorded. That is the loss case, and step 4 cannot repair it — it is reported, per _What a
   consumer does with a gap_ above.

What this does **not** give on its own is exactly-once _effects_. Recording a frame once is the
prerequisite; a reply produced from it reaching the room once is stage 5's outbox, and the durable
cursor beside the fold is stage 4's. This stage makes the log's own identity trustworthy so those
two have something to key on.

#### The release schedule, and which parts are additive

`maxUnavailable: 0` means old replicas run against the new schema for the length of every roll, so
R3 is gated on R2's roll having **converged** — every pod on an image at or after it — rather than on
a release having elapsed. A stalled roll leaves the old replica serving, which is exactly when "one
release later" is the wrong gate (the same discipline `session_notifications.py` documents for the
channel rename).

| Release               | What lands                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Additive?                                                                                                                                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **R1**                | The wire and the runner: optional `seq` on the runner→console envelopes, optional `resume_from` on `start`, the runner minting and replaying from the cursor. Separately (no ordering between them): `cli_type` added, dual-written, every reader onto `coalesce(cli_type, kind)`, backfilled where `kind <> 'setup_output'`; and `session_frames.runner_seq` recorded with a partial index, plus the console computing `resume_from` from it.                                                                                                     | **Yes.** New optional fields, new nullable columns. Old replicas and old runner images unaffected.                                                                                                             |
| **R2 — the cutover**  | The sink moves to `WebSocketTransport`; `kind` becomes the envelope discriminator, typed `BridgeFrameKind`, with the coalesce dropped and the CLI's own `type` left in `cli_type`. `frame_ord` and the composite primary key `(session_id, frame_seq, frame_ord)`; `Identity` demoted to a plain sequence default. Frames take `frame_seq` from the envelope, dedup keys on position rather than `frame_uid`, both density checks report, `REPLAY_WINDOW` is re-sized, and the `partial` row and `runner_seq` stop being written and are unmapped. | Schema yes, behaviour no — the `kind` flip is safe **only** because R1 stopped every reader depending on it. An old replica still filtering `kind = 'assistant'` would mis-decide a turn in `adopt_open_turn`. |
| **R3 — the contract** | Everything an old replica was still touching through R2's roll: `frame_seq`'s sequence default and the sequence behind it dropped, so an insert that names no number fails instead of inventing one; `runner_seq`, the `partial` column and their indexes dropped.                                                                                                                                                                                                                                                                                 | Removals, so gated on R2 converging.                                                                                                                                                                           |

**Two releases, and it is replicas that force the second rather than rows.** An old replica inserts
without naming `frame_seq`, and still writes `runner_seq` and `partial` — so R2 cannot drop what it
would need. That is why `Identity` is demoted to a plain default rather than removed (§ _The
primary-key question_), and why a column SQLAlchemy still names in every `SELECT` is unmapped in R2
and dropped in R3 (<../console/README.md> § Perimeter / deploy). Nothing about the rows already in
`session_frames` extends the schedule past that: they are deleted, not migrated.

**The gate is one fact, checked in two places.** Every `haku-console` pod is running an image at or
after R2. The desired tag is in
<../../cluster/k8s/haku/console/deployment.yaml> in two places, both rewritten by Flux's
`ImageUpdateAutomation`: the `server` container's `image:`
(`{"$imagepolicy": "flux-system:haku-console"}`) and the `HAKU_CONSOLE_IMAGE_TAG` value (the `:tag`
variant of the same policy). The tag is `devel-<UTC build stamp>-<short sha>`, so "at or after R2" is
`git merge-base --is-ancestor <R2's merge commit> <that short sha>` — mechanical, with no judgment in
it. But desired is not running, which is precisely what `maxUnavailable: 0` buys: a replacement that
never becomes Ready leaves the previous replica serving indefinitely. So the running half is its own
check:

```bash
kubectl -n haku-console get pods -l app.kubernetes.io/name=haku-console \
  -o jsonpath='{range .items[*]}{.spec.containers[?(@.name=="server")].image}{"\n"}{end}' | sort -u
```

One line, equal to the manifest's `image:`. Two lines means the roll has not converged and R3 is not
eligible, whatever the manifest says.

**Done when** `kind` answers one question, `BridgeFrameKind` is its type, and `frame_seq` is the
number the frame crossed the wire under — the only number any row carries.

##### The old rows are purged, not carried

`session_frames` is a permanent log, so "stop writing identity numbers" would otherwise leave every
session recorded before R2 numbered that way forever. They are deleted instead, on the operator's
authorisation (2026-08-16):

> I would be okay with dropping all data in the production database that relies on the old incorrect
> numbering of frames if it makes things easier. there is really not much more there than just test
> conversations, nothing in the conversation tables I'd really miss in Haku console db

`DELETE FROM sessions` is the whole operation. `ON DELETE CASCADE` takes
`session_{frames,messages,turns,prompts,events,outbox}`; `session_turn_prompts` and
`matrix_held_batch` cascade from the message and turn rows going with them, and a held batch's
disappearance is the safe direction, since the watermark never moved and the homeserver re-offers it.
`matrix_conversation.session_id` is set NULL, after which the Matrix supervisor provisions a
replacement session against the same room — which is what makes the operation survivable on a live
room rather than only on terminal sessions. Run against production on 2026-08-16 it removed 302
sessions and 35,795 frames, and a replacement session was recording four minutes later.

**Run it on both sides of R2's roll.** Before, so no live session carries identity-numbered frames
into the roll at all. Again once R2 has converged, because the roll is itself a window in which an
old replica can create a session and record identity frames into it, for a new replica to adopt and
compute a useless cursor over (§ _The primary-key question_). The window is the roll's length and
what it costs is one session's replay. If that is too sharp to accept, the discriminator to refuse on
already exists and R3 drops it anyway: a session holding a row with `runner_seq IS NOT NULL` is one
an old replica recorded, so closing it lets the supervisor provision a replacement instead of
adopting a log the new code cannot number. A session whose entire recorded log is narration has no
such row and also has nothing to lose.

Outside the console's own tables, one consumer holds pointers with no foreign key: the index's
`chat_{sessions,chunks,chunk_messages}` live in their own schema and reference `session_id` as a bare
UUID. **It repairs itself and the window is one sweep.** `sync_chat` computes
`forgotten = indexed sessions − sessions the source still shows` and calls `forget_chat_sessions`
before indexing anything, and the chat sweep runs every minute. What a reader sees until then is a
`haku_index.search(corpus=conversations)` hit whose drilldown fails; afterwards the session is not
listed by `haku_conversations.list_conversations` and `read_{transcript,rollout,frame}` on its id
fail rather than return an empty conversation. **The Matrix room still has the conversation** — room
history lives on the homeserver and is federated, so the purge reaches the console's record and
nothing else. For a room-attached session that asymmetry is what dropping means.

##### The invariant the read path owes

**Nothing in the read path may assume `frame_seq` is 1-based, comparable across sessions, or by
itself a row identity.** After R2 every session's numbers do start at 1, which is exactly the moment
someone writes a reader that relies on it — and `frame_ord` means one `frame_seq` can name more than
one row of a session. `FrameCursor`, `read_frames`, `read_operator_frames`, `session_turns`,
`session_messages.source_*`, the MCP tools and the frames page use `frame_seq` as a per-session total
order and nothing more; a test over a session with deliberately sparse, high-valued frames and a
console-origin row at `(N, 1)` states that on purpose rather than leaving it true by accident of how
the readers were written. The fixture stays sparse even though production holds no sparse session: it
is a statement about what the readers must tolerate, not a sample of the table. Density has exactly
two consumers — live contiguity checking and `resume_from` catch-up — and both need a runner attached
to the socket, which no closed session has.

### 3. Turn state onto the turn row — **done**

`session_turns` carries `assistant_message_id`, `said_anything` and `queued_reply` (migration
`0043`), each written in the same transaction as the effect it describes: the message pointer with
`begin_assistant`, the other two with the `update_assistant` that completes a message and inserts
the room's outbox row. `_run_turn` reads them through `SessionStore.turn_state` at the top of every
turn, so a turn this process opened and one it adopted enter the loop the same way. `TurnResumed`
became `ResumedTurn`, which carries only a `turn_id`; `_said_anything` and `_streaming_assistant`
are gone.

`spoke` is now `queued_reply`: **an outbox row exists for this turn**, recorded by the statement
that inserts it. Not `sent_at`, which is the drain's answer to a different question — an unsent row
still means the room is owed the text and must not be queued a second time — and not the "we
attempted" a delivery call used to report. `said_anything` is its own field rather than `spoke`
reused, because a session with no room queues nothing: conflating them was what let a resumed SPA
turn mint a second message for `result.result`.

What is left for stage 4: `adopt_open_turn` still asks the frames _which_ of its three cases a turn
is, and the loop still holds each value between two writes of it rather than folding a frame into
state. The row is now both halves of what the fold needs — `first_frame_seq` says where the turn's
frames begin, the three columns say what projecting them has produced — so what stage 4 adds is the
cursor between them.

### 4. The fold, and what it projects **into** — conversation landed, session events begun

**What it projects into is built, the fold and its cursor are wired, and the conversation events
are rows.** #4145 landed the neutral vocabulary (`x/conversation_events.py`), the Claude adapter
into it (`x/claude_code/projection.py`) and a read surface over the result (`read_transcript`);
#4149 made the adapter a reducer; the turn loop and the room's status line both read it; the
durable cursor is `sessions.projected_frame_seq` (§ The shape); and `session_events` stores what
the fold produces, written inside the cursor's own transaction. **All four interpreters counted
below are gone.** The _second_ category — session events, which cross no wire — has its first two
writers, with most of its vocabulary still to come; the paragraphs that have landed say so where
they are.

`_run_turn`'s frame `match` became `project`, with the cursor advanced beside its effects. The
abort path is still to collapse here: an abort becomes an intent the transport writes, and the
CLI's answer comes back as frames — which is what removes the "exactly one `anext` in flight"
dance.

An earlier draft called this "mostly moving code once stage 3 has made the state durable". That
undersold it. The fold is not a relocation of the `match`; it is where the system decides **what a
message is**, and that decision is currently made four times in four places.

#### The four interpreters, counted

Frames are authoritative: they are what the runner protocol plus the inner CLI's protocol put on the
wire. Everything else is derived — but nothing says so, so four bodies of code read Claude's frame
vocabulary independently:

| Where                             | Reads                                 | Produces                                        |
| --------------------------------- | ------------------------------------- | ----------------------------------------------- |
| `_run_turn`'s `match`             | `assistant`, `stream_event`, `result` | message rows, live                              |
| `adopt_open_turn` and its helpers | the same, from the log                | the same, reconstructed                         |
| `session_views.rollout_calls`     | `assistant`, `user`                   | tool calls **and their results**, on every read |
| `room_status.coarse_status`       | `assistant`, `system`/`task_started`  | the room's status line                          |

The third exists because `session_messages.tool_calls` is a **half copy** — the calls without their
answers, because the turn loop drops the `tool_result` blocks — so the read path re-derives both and
prefers its own answer wherever the row can point into the log. The fourth means a backend whose
frames are spelled differently makes the room go silent while the agent works, and it is why the SPA
has no in-progress display at all.

**All four are gone.** `_run_turn` acts on events, `coarse_status` reads a run of them rather than
a frame, `adopt_open_turn` no longer reconstructs anything — a resumed turn's remaining frames go
through the live loop from the cursor — and `rollout_calls` is deleted, because the calls and their
results are rows that the read path looks up rather than derives. The status line cost one branch
on the way:
`system/task_progress` had a case there and has none in the adapter, which is a frame class the
census has never seen (<../console/x/README.md> § The neutral projection).

#### A message is provider-neutral, and tool calls are in it — **built**

The vocabulary below is `x/conversation_events.py` (#4145), event for event, and
`x/claude_code/projection.py` produces it. Two shapes the argument did not anticipate and the wire
forced: a tool result's content is a variant (`TextContent | ToolReferences | OpaqueContent`)
rather than a string, and `Outcome.UNKNOWN` exists because `is_error` is _absent_ rather than false
on most real results. Both are findings from <../console/debug/frame_shape_census.md>. The
reasoning below is kept because it is what the vocabulary answers to.

The alternative — messages as prose only, tool activity left in frames — was considered and
rejected, on an argument from what the channels already do (operator, 2026-08-16). **Matrix shows
tool calls in progress.** If that display is to come from a neutral source rather than from one
CLI's frame types, then tool calls have to be in the neutral layer, and as a **lifecycle** rather
than as completed records stapled to a finished message:

- `TextDelta`, `MessageCompleted` — what was said.
- `ToolCallStarted(call_id, name, input)` → `ToolCallCompleted(call_id, result, is_error)`.
- `Activity` — the harness's own prose for a step in flight (`task_started`'s `description`), which
  is the case that has no tool name at all.
- `Reasoning` — the agent thought, with a summary where it gave one. A distinct state rather than
  empty text: Claude emits `thinking` blocks and Codex emits reasoning summaries, and a thinking-only
  message currently renders blank, which is a live bug rather than a hypothetical.
- `TurnCompleted` — with a **neutral** usage shape. Today `end_turn` stores Claude's `result` payload
  as it arrived; leave that and "turn completed" quietly means "whatever that one CLI sent".

This is not a second copy of the wire. It is a normalization, and the difference is that a copy
duplicates a shape while a projection **replaces** one — the harness's content-block encodings,
thinking signatures, stream-event mechanics and `msg_…` id formats stay in frames as evidence, and
none of them appear in a message.

**Identity is ours.** `rollout_calls` joined on `agent_message_id`, the agent's own id — and a
production count found 1,417 assistant rows that have none, which rendered their tool calls with no
results and said nothing about it. The neutral message owns `message_id`; the agent's id is optional
provenance. This is the lesson `EventTag.transaction_id()` taught in stage 5: identity derived from
the wire fails exactly when the wire does not carry it.

#### Two categories, one ordered stream — the second category has its first writers

`ConversationEvent` is the first category and nothing else: `x/conversation_events.py` carries what
participants said and did, and carries no session event at all. The second is stored beside it
rather than modelled there — `AuthoredEventKind` plus a body per kind — because what a session
event needs is a durable row and an order, not a place in the fold's own vocabulary. The split
below is otherwise still owed, and with it `RoomEventKind`'s move out of
`channels/matrix/client.py`.

Bootstrap narration is the case that shows the vocabulary above is incomplete (operator's question,
2026-08-16). Sandbox setup output is **not the model talking** — it is the bridge: the haku-state
checkout, workspace preparation, and the CLI's own stderr, arriving as `SetupOutput` envelope frames
in the runner's vocabulary rather than the inner CLI's. It has no turn and no speaker. But both
channels want it — the room narrates it live, the SPA is getting it — so it cannot stay a
channel-side interpretation of a raw frame.

So the neutral stream carries two categories:

- **Conversation** — what participants said: operator messages, agent messages, tool activity,
  reasoning.
- **Session events** — what happened _to_ the session: provisioning, bootstrap narration, a session
  ending and being replaced, a held batch, an aborted turn's notice.

**One stream, because ordering is the point**: narration interleaves with messages in the room, and
the debug view above reads in sequence. **Two categories, because every consumer filters
differently**: the index embeds conversation and not "provisioning a sandbox", the surfaces render
them differently, and a classifier cares about outgoing agent text rather than bootstrap.

`RoomEventKind` — `REPLY`, `NARRATION`, `LIFECYCLE`, `STATUS`, `HOLDING`, `ROOM`, `UNREADABLE` — is
already that enum, and it lives in `channels/matrix/client.py`: a neutral concept inside a channel-specific
module, the mirror of the smell `coarse_status` had — a neutral module reading one backend's wire.
It moves into the neutral layer, and channels render categories rather than deciding them. That also
delivers what <../console/plans/session_channels.md> asks for — session-level events recorded rather
than existing only as Matrix notices — because a notice becomes a record both channels read.

**A third origin, and it qualifies "frames are authoritative".** Some session events cross no wire
at all: the replica owning a session changing hands — a lease taken, expired, or adopted — is a
console-side fact with no frame and never will have one (operator's question, 2026-08-16). So the
stream has three origins: the inner CLI (conversation), the bridge (setup output), and **the console
itself** (ownership, lifecycle). Frames are authoritative for _what crossed the wire_; a
console-origin event is authored directly and is its own evidence.

Two consequences, and the first is a trap:

- **Reprojection must preserve rather than re-derive them.** Re-projecting a session's frames cannot
  rebuild an event that was never in them, so a naive rebuild-and-replace would silently delete every
  ownership change while the check reported green. What keeps `check_session` honest without it
  having to know the category exists: an authored row names no turn, and the check reads a turn's
  rows.
- **Provenance is a union, not a nullable range.** `frame_range | authored` — an authored event has
  no range because it has no frames, which is different in kind from a frame-derived event whose
  range is unknown.

**The principle that settles where these rows go** (operator, 2026-08-16):

> i think the right thing would be: frames only come from actual runner<->console communication.
> events like "session taken over by this replica of console" are not that. so they would probably
> arrive as a different sort of event.

**The frame log is the record of runner↔console traffic and nothing else may enter it.** That is
what decides against <../console/plans/session_channels.md> § 3, which would have made a lifecycle
event a frame-log row under its own bridge-side `kind`: a lease changing hands crosses no wire, so
such a row is an envelope invented to fit, and a reader of `session_frames` would have to learn
which of its rows are not evidence of anything said. Bootstrap narration is not that case and keeps
its frame — a `SetupOutput` envelope is runner→console traffic.

**Two facts have a writer**, each in the transaction that makes it true: a replica taking a session
over (`session_store.authenticate_bridge`) and a lease lapsing past the adoption grace
(`expire_stale_leases`, which also records _which_ of its three cases ended the session).
`session_events.turn_id` became nullable for them — a session that died before it ever reached a
turn is exactly what this category exists to record, and it has no turn to name. The rest is
unbuilt: `_SessionStatusAnnouncer`'s transitions, the held batch, the unreadable inbound event, an
aborted turn's notice.

Worth recording because it is cheap and would already have paid: three hypotheses in the
2026-08-15 drop investigation turned on whether a console had rolled, and the available evidence was
the operator's recollection. Ownership in the stream makes that a query. It is also the clearest case
of **recorded but not rendered** — it happens on every deploy, so it belongs in the log and the frame
inspector rather than in a room or a transcript.

**Status is not an event.** The typing indicator and the "running Bash" line are derived state —
what is happening _now_ — and fall out of projecting the stream rather than being entries in it.
Storing them would litter every transcript with status churn.

#### The projection is not a one-way door

**From the SPA, an operator must be able to click a message or an event and read the actual
provider-specific frame JSON behind it** (operator, 2026-08-16). A normalization that cannot be
appealed is a normalization nobody can debug — and the whole reason for keeping frames authoritative
is that they are the record the projection can be checked against.

So every projected thing carries its provenance: the frames it came from, addressed by
`frame_seq`, not by the agent's own ids. That is #4105, **landed**:
`session_messages.source_first_frame_seq`/`source_last_frame_seq` (migration `0045`), surfaced on
`SessionMessageView`. This plan makes it a **product requirement** rather than the diagnostic
convenience it was proposed as — and it should extend to tool calls and activity, not stop at
messages, since those are exactly the elements whose neutral form loses the most detail.

**The extension landed for what is read, not for what is stored** (#4145). Every
`ConversationEvent` carries a `Provenance`, and `read_transcript` hands it out on every entry —
tool calls and activity included — so an operator can appeal any of them to the frame behind it.
But the projection is computed per read and no neutral row exists yet, so this is provenance the
fold _derives_ rather than provenance the database _holds_. The stored half arrives with the table
stage 4 has still to build, which is also where the `CHECK` below belongs.

**Making the range required, without dropping history.** A nullable range that sometimes means
"unknown" is the weak version of this, and the operator asked whether to recover history or drop it
so a `CHECK` can require the range (2026-08-16). Neither, yet — take the middle:

- **`ADD CONSTRAINT … CHECK (…) NOT VALID`** when provenance lands. New and updated rows must carry a
  range; existing rows are tolerated and unchecked. That stops the problem growing, which is the part
  that matters, and `VALIDATE CONSTRAINT` promotes it later without rewriting the table.

  **Restated, having been attempted (#4143).** #4105 shipped only the ordering half — that
  `first <= last` when both are present — so a row with no range at all stayed writable. Closing
  that against `session_messages` turns out not to be possible, and the reason is the same union
  three paragraphs above: **NULL in those two columns means three different things and a `CHECK`
  sees one.** A row predating #4105; the operator's prompt, written before the frame it goes out as
  exists and never pointed at all if no turn claims it; and a projection whose frame carried no
  sequence, since `ReceivedFrame.frame_seq` was `int | None` — a `ClaudeCli` with no rollout sink
  numbered nothing. The first two are the `authored` arm, the third was
  `frame_range`-but-unrecorded — **and the third is now gone**, closed by the numbered-frame
  paragraph below. Two meanings a `CHECK` still cannot tell apart is one fewer than three and is
  still not one. A discriminator column would separate them, but
  it would be dead on arrival: `session_messages` has no `authored` writer today (the operator's
  prompt does acquire a frame), so the column would carry `frames` ⟺ `first IS NOT NULL` and
  nothing else — the range restated.

  **So the requirement moves to the neutral events**, where the union is a real discriminator
  rather than a NULL, and it should land with the table that stores them rather than being
  retrofitted here. What `0046` does ship is the part that is unconditional: **a far end with no
  near end** — neither a range nor the absence of one — which every writer already satisfies, so it
  is roll-safe under `maxUnavailable: 0`.

  **The `int | None` was the seam, and it is closed.** The console's only client is built with a
  `RolloutRecorder` unconditionally (one construction site, no branch), so an unnumbered frame was
  never a state production reached — but nothing said so in a type, #4134's adapter fabricated a
  placeholder sequence (`_UNNUMBERED_FRAME = -1`) to work around it, and a would-be constraint had
  to tolerate it. `ClaudeCli` now takes its `FrameSink` as a required argument, so
  `SentPrompt.frame_seq` and `ReceivedFrame.frame_seq` are `int` and the placeholder is deleted.
  A numbered frame at the console's boundary is what makes the requirement expressible at all, and
  it is the prerequisite `frame_projection.projected`'s docstring named for threading a
  `ProjectionState` through
  the turn loop.

  **Who assigns the number is settled, and it is the runner** (operator, 2026-08-16). § 2b holds
  the design: a dense counter minted where the frame goes on the wire, carried on the envelope, and
  handed back as `resume_from` on reconnect. The number stops being something a sink reads back
  after an `INSERT` and becomes something the frame arrives carrying — which is what makes catch-up
  a replay from the runner's own window rather than a reconciliation against the table.

- **Backfill falls out of `reprojection.check_session`** rather than being its own archaeology:
  project a session's frames, align the derived sequence against the stored rows, and write the
  range where the alignment is unambiguous. Where it is not, that is a finding about the projection
  rather than a gap to guess at.

  `0045` took a cheaper first pass that is consistent with this rather than a substitute for it: it
  filled the range for assistant rows that carry the agent's own message id, by joining that id
  against the `assistant` frames. That is precisely the unambiguous case, and it is bounded by the
  same defect the neutral message exists to remove — a row with no agent id gets nothing, which is
  the population the reprojection backfill still has to reach.

- **Bounded by frame completeness.** Stage 1 is what made every frame class recorded; before it the
  log has holes, so no range is recoverable there at all. The boundary is checkable rather than
  guessable, and the full `VALIDATE` can only ever cover the post-stage-1 era.

**Dropping history is more expensive than it looks**, which is why it is not the recommendation:
`session_messages` _is_ the `haku_index` chat corpus — `chat_source.py` embeds `role`, `content` and
`created_at` for semantic recall — so dropping it deletes Haku's memory of past conversations. That
may still be worth doing one day, but as a deliberate decision rather than a side effect of wanting a
`NOT NULL`.

Two things fall out of the same pointer, and the first has happened: the transcript's join to tool
activity is a range lookup rather than a scan-and-match on `agent_message_id`, which is what let
`rollout_calls` retire. And the reprojection check below has a per-row subject: not just "do the
rows match" but "does _this_ row match what _those_ frames project to".

#### What makes it safe: the projection is a pure function, and that is testable

The property that prevents drift is not "do not duplicate" but **determinism**. If `project` is a
pure function of a frame sequence, then:

- drift is **detectable** — re-project a recorded session's frames and compare against its stored
  rows, over real sessions, in CI;
- a projection bug is **repairable** — fix the fold, re-project, and the transcript is corrected,
  rather than a row written wrong staying wrong forever;
- and the rows with no agent id stop being permanently degraded, because the rebuild does not depend
  on the agent having supplied one.

Stage 5's outbox already relies on the fold being single-writer per session (see the closing note);
reprojection is the other half of that bargain and wants writing at the same time.

**Built, as `x/reprojection.py`'s `check_session`** — a function returning findings, with three
things the paragraph above did not anticipate. It must fold **as the write path configures the
fold**, per frame and under `STREAM_EVENTS`, because `project_log` over a whole session is a
different event sequence and a checker driving it reports drift everywhere; that is why the fold is
now `x/frame_projection.py` rather than the turn loop's private function. It must run **per turn
and skip a turn with no rows at all**, because that is what a replica on the image before these
rows existed leaves behind, and without the skip every live session predating the release would be
reported as drifted until that session ends.

**No standing check and no CLI over it** (operator, 2026-08-16). It cannot run in CI — what it
needs is production rows — and production has almost none: `session_events` held **one row** on
2026-08-16, a `message_completed` over frames 45129..45129, every other session predating the
writer. A tool pointed at that corpus reads nothing, and the backfill above — the one caller it had
— is deleted with the rows it was built to point
(<../console/debug/2026_08_16_legacy_purge.md>). So this is a function, and
the CLI is a decision to revisit only if a drift report ever has a population to speak about.

#### Pressure-tested against the two things that would break it

Neither is implemented; both are read from documentation rather than measured, in the same spirit as
<../runtime/x/bridge/docs/second_backend.md>.

**A Codex backend.** Forces the four points above: identity cannot be borrowed; reasoning is a state
both harnesses have; `TurnCompleted` needs its own usage shape; and the status line has to derive
from neutral events or the room goes quiet. What it does **not** force is an approval concept —
approval requests and responses travel over MCP to the console's queue, not over this channel
(operator, 2026-08-16), and a harness that wants to ask about commands is configured not to in its
launch spec, which is what `CliBackend.resolve` is already for.

**A Telegram channel.** Breaks something different and sharper: **`sendMessage` has no idempotency
key.** The outbox's retry is safe against Matrix because a redrive reuses the transaction id and the
homeserver refuses it; against Telegram an ambiguous timeout genuinely double-posts. So a channel
port must **declare** whether it has an idempotency key, and R11.6's "possibly duplicated" marking —
deliberately not implemented in stage 5 because a stable transaction id left it no case to fire on —
is exactly what a channel without one brings back. Telegram also caps a message at 4096 characters,
so one neutral message can be several channel messages: "sent" is a property of a _(message,
channel)_ pair that may hold more than one remote id.

What survives both, unchanged: frames as per-backend evidence, the outbox as rows with a cursor, and
content as neutral markup rendered per channel — `channels/matrix/formatted_body.py` already does the second half of
that, so the channels share a source and not a rendering.

#### Does a turn live over frames or over neutral events?

Over the neutral events (operator's question, 2026-08-16) — but the question is worth answering
carefully, because today the two are indistinguishable and the code picks the wrong one.

`session_turns` opens when a prompt is claimed and closes on Claude's `result` frame, whose payload
is stored as-is for cost, usage and duration. So the boundary is currently **one CLI invocation**.
Every consumer, though, is neutral: the room's typing indicator and status line bracket a turn, the
SPA renders turn boundaries inline in the transcript, the outbox keys a turn's last word by
`turn_id`, adoption asks whether a turn is open, and I3 wants a batch acknowledged when its turn
completes. None of them care how a particular CLI spells the end.

**The concept is three jobs wearing one name**, which is why it is worth being careful before
moving it. Counted from its readers:

| Job                                                 | Reader                                                                                                                        | Belongs to                                                 |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **A mutex** — one exchange at a time                | `enqueue_prompt` refuses while a turn is open, which is what makes queue-until-turn-end (R2.2) work                           | the harness cycle: one CLI cannot take two prompts at once |
| **A recovery marker** — this exchange was abandoned | `adopt_open_turn`; the row already carries `first_frame_seq`, so a turn is quietly a frame range                              | the harness cycle: an invocation can be interrupted        |
| **An accounting and display unit**                  | `list_turns` for the SPA's inline boundaries, outcome, duration and cost; `uq_session_outbox_turn` for one last word per turn | the conversational exchange                                |
| **"Is the agent working"**                          | `responding = await _open_turn(...) is not None` — the status column no longer carries this                                   | both, today                                                |

Mid-turn steering is exactly the change that separates them: the mutex relaxes (a second message
joins a running exchange) while the display unit stays one turn. So when the split comes, the mutex
and the recovery marker follow the invocation and the accounting unit follows the conversation —
and "is the agent working" becomes a question about the invocation, since that is what the typing
indicator is actually reporting.

**Two concepts coincide today and need not always.** The _harness cycle_ — one prompt in, one result
out, carrying that invocation's usage — and the _conversational exchange_, which is what a person
means by a turn and what every surface renders. They are the same thing only because one batch makes
one prompt which makes one invocation. Three plausible changes separate them: mid-turn steering
(folding a message into a running exchange, deferred but planned), a backend needing several
invocations for one exchange (continuation, compaction, an internal retry), and a harness reporting
usage per invocation across an exchange that spans more than one.

So the turn is neutral, its boundaries are **produced by the backend adapter** (`TurnCompleted`), and
if the two ever diverge the neutral turn is the conversational one while "which invocations made it"
becomes frame-level detail — reachable through the same `frame_seq` range as everything else. Two
consequences: the neutral usage shape should be **aggregatable** rather than one payload, since an
exchange may sum several invocations; and `end_turn` storing the raw `result` stops being "what the
turn cost" and becomes evidence, with the cost living in columns that mean the same thing on every
backend.

**Both have landed** (migration `0049`). `end_turn` takes the adapter's `Usage` and writes
`input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd` and `duration_ms`; the raw
`result` payload is evidence in `session_frames` and nothing reads it as the answer any more —
`adopt_open_turn` included, which used to close a recovered turn on `is_error` and so reported
every finished turn as answered. Aggregation is not uniform, which is the part the requirement
hid: the counters and the cost sum (a NULL cost makes a total unknown rather than smaller), while
`duration_ms` does not — wall clock of invocations that may overlap is not their sum, so an
exchange's own span stays `ended_at - started_at`, which the console measures and no backend
supplies.

#### Two decisions this leaves open

- **The SPA renders messages by default and must let an operator inspect the tool calls underneath**
  (operator, 2026-08-16). That is disclosure over one neutral source, not a second query path — but
  which surface shows what, and whether in-progress calls appear in the SPA the way they do in the
  room, is unbuilt.
- **Reasoning and tool activity are not rendered in the channels or the SPA conversation view for
  now** (operator, 2026-08-16). They are projected and readable; showing them is a later decision. Note
  what that implies: the neutral layer carries strictly more than any surface currently displays,
  which is the right direction — a projection that only holds what today's UI renders would have to
  be re-derived the moment a surface grows.
- **Durable tool inputs and results widen the read surface.** `read_transcript` already hands an
  agent a tool call's arguments and its result (#4145), derived per read; making them rows only
  widens what is durable, not what is reachable, so the policy question below is live now rather
  than when the table lands.
  <information_trust_tiers.md> reasons about who may read past conversations; it will have to reason
  about who may read past tool activity. The index should keep embedding prose only — tool JSON
  would pollute the vectors — but that is a selection choice, not a boundary.

### 5. The room outbox — **done**

`session_outbox` holds each produced reply until the homeserver has taken it;
`channels/matrix/outbox.py`'s `RoomOutboxDrain` says it, under an advisory lock, through the pacer, marking it
sent only after `room_send` returns. The pacer kept its deque and its budget: it is still
what decides _when_, and the console's narration still lives on it. What follows is what the
stage was written against, kept because the reasoning is still the design's.

**One correction to make before reading further.** "The transaction id is already there" below is
half true, and the half that is false is what a redrive would have broken.
`EventTag.transaction_id()` derives from the transcript row _only when there is one_ and mints a
fresh `uuid4()` otherwise — so a redelivery of a turn's abort notice, or of text that arrived only
on a `result` frame, would have posted a second message rather than being refused. Every outbox
row is therefore sent under **its own id**, which is stable for exactly as long as redelivery can
happen, and two partial unique indexes (on `message_id` and on `turn_id`) stop a second row being
created for one logical reply.

**Not done, and deliberately deferred: R11.6's "mark a late reply as possibly duplicated".** With
a stable transaction id inside Synapse's 30-to-60 minute dedup window, a late redelivery is
refused rather than duplicated, so the marking has no case left to fire on for anything the outbox
sends; past that window it would, and the retry budget is sized to stay inside it. Revisit if the
budget grows or if a channel without transaction dedup is added.

The drop audit (<../console/debug/message_drops.md>) turned this from the last stage into the one
with a live bug behind it, and gave it three constraints it did not have when this was written.

**Why the layer above cannot be made honest without it.** `MatrixSyncService.reply` appends a
closure to an in-process deque and returns, so `deliver` reports success before any HTTP request
exists. Whatever the turn loop records at that moment is a statement about a `deque.append`. The
`spoke` fix that landed makes the turn believe the surface rather than itself, which closes the
failures the surface can see; every failure the pacer meets afterwards — a 429 past two retries, a
5xx, a `login` the token closure needed — is still popped and discarded with a log line and no
retry, ever.

**The generalization to aim at, from <../console/plans/session_channels.md> § 1: a channel is
reconciled against the session rather than sent to.** The outbox is the push half — deliver
promptly; a per-attachment cursor on `chat_attachment` is the convergence half — deliver
eventually and at most once, with repair as the normal path rather than a retry bolted on. Two
consumers want the pair. Once lifecycle events are recorded rather than only announced, "the
room's queue" becomes "bring each attached channel up to this session's record". And relaying an
operator's console-sent message into the room stops being a feature at all: it is a transcript row
the room lacks, which is the same divergence as an undelivered reply, so the loop already closes
it. Build the cursor with the rows.

**A third consumer changes this stage's priority from tidy-up to prerequisite.**
<information_trust_tiers.md> puts a classifier in front of the drain, so that outgoing messages
are checked before they reach a room rather than after they have federated — and a classifier
needs somewhere durable to hold a message while it decides. An in-process deque cannot be that.
The queue is also what makes the check asynchronous with respect to the agent while staying
strictly before the room, which is the property that made the design worth having; the ordered
drain is what makes "the first failed message halts" a rule rather than a race.

**Write the row where the reply is produced, not where it is delivered** — in the same transaction
as `update_assistant`. At delivery time it does not survive a turn that raises between producing
text and speaking it, which is a real path today (audit E4) and would quietly stay open.

**The transaction id is already there.** `EventTag.transaction_id()` makes redelivery idempotent
server-side, so a redrive sweep is safe to write on day one rather than needing a dedup design
first.

**What it did not close, and what has closed since.** The outbox was never going to fix the abort
drain discarding a produced reply (E3) or the ingress side's acknowledgement semantics (I2–I3);
each wanted its own change, and each has had one. E3 is fixed — an aborted turn keeps the message
it had already finished (#4109); I3 is fixed — a batch is acknowledged after its turn rather than
at its enqueue (#4117); and before those, the unmappable event is announced rather than dropped
(I1, #4087) and an empty turn says so (E5, #4088). The audit
(<../console/debug/message_drops.md>) is clear apart from I2's residual windows, which it records
as re-deliveries rather than skips.

## The one thing to keep in view

**The projector must be single-writer per session.** The lease gives that, and it is the reason none
of this needs the fold to be re-runnable. An expired lease now means unowned rather than dead, but
the property still holds: `authenticate_bridge` admits one holder at a time while a lease is valid,
and expiry only makes the row adoptable — it never lets two projectors write at once. A future change
to the lease's meaning should be checked against this assumption rather than around it.
