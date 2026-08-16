# The month ahead — the chat runtime

**A schedule, not a design.** It orders <chat_runtime_projection.md>,
<chat_runtime_cleanup.md>, <../console/plans/session_channels.md> and
<../console/plans/one_read_api.md>; where it disagrees with one of them it says so and the design
document is what moves. Nothing here restates a design. Delete this file when the month is over
rather than letting it rot into a second backlog.

The previous month's burn-down was a different document with a different job: it tracked a
sequence of fixes to a runtime that worked but was shaped like the patches that built it. That
work is done and the plans it burned down have been reconciled against the code
(<../console/debug/2026_08_16_plan_code_drift.md>). This month has one thesis instead of five
tracks.

That burn-down never reached `devel` — it lived only on #4078, which the operator kept as a
working document. This file replaces it at the same path, and carries forward the two things in it
that outlived their month: the outbox rule below, and the directory hazards in item D.

## The thesis

**The projection exists and nothing is stored through it.** `x/claude_code/projection.py` is the
one interpreter that four bodies of code were supposed to collapse into, `conversation_events.py`
is the vocabulary, the turn loop drives its live path off both (#4134), and the MCP transcript
reads through them (#4145) — but every row the system keeps is still written by the code that was
there before, and every recovery path still reconstructs state from Claude's frames.

So the month is about closing that gap, in the one order that works: **make the projection
resumable, then give it a durable position, then let it own the rows.** Everything else is either
a consequence of that or is genuinely parallel to it, and the parallel work is dispatched rather
than queued behind it (`AGENTS.md` § Splitting Work Into PRs).

The risk to steer around is the opposite of last month's. Last month the danger was shipping
features onto a structure that could not hold them. This month the structure is nearly right and
the danger is **building the storage layer before the shape is settled** — a table of neutral
events written against a projection that still cannot resume is a migration that has to be
redone.

## Where this starts

In flight, and treated as in-flight rather than as new work:

| What                                            | Where | Note                                                                                                                              |
| ----------------------------------------------- | ----- | --------------------------------------------------------------------------------------------------------------------------------- |
| The projection becomes a reducer                | #4149 | **Landed.** Spine item 1                                                                                                          |
| A numbered frame at the console's boundary      | #4164 | **Landed.** Spine item 2 — `_UNNUMBERED_FRAME` deleted                                                                            |
| The status line reads events, not Claude frames | #4162 | **Landed.** Parallel item A, with the observable status text unchanged                                                            |
| `claude_bridge/` → `bridge/`                    | #4141 | **Landed.** The runtime half of the directory finish                                                                              |
| Block style where Flux rewrites, plus the guard | #4147 | **Landed.** Not chat-runtime work, and it matters anyway: it is what stopped unrelated PRs going red on a file they never touched |
| The runner numbers the frames it sends          | #4166 | **Landed.** R1's wire-and-runner half; § 2b of <chat_runtime_projection.md> holds the schedule                                    |
| Retiring identity numbering scheduled as R5     | #4167 | **Landed.** The gate is two observable halves — Flux convergence and no live identity-numbered session                            |
| A turn's cost is columns, not a CLI payload     | #4169 | **Landed.** Parallel item B                                                                                                       |
| The console records the runner's number         | #4172 | **Landed.** R1's console half: `runner_seq` recorded, `resume_from` computed from it                                              |
| The durable projection cursor                   | #4178 | **Landed.** Spine item 3 — `sessions.projected_frame_seq`, migration `0051`                                                       |
| R5 renumbers historical frames                  | #4170 | **Landed.** R5 renumbers where every reference has an image and drops the sessions where one does not                             |
| Docs concision across `haku/console/x/`         | #4171 | **Landed.** No executable line changed; AST-identical to `devel` with docstrings stripped                                         |
| The neutral events get rows                     | #4179 | In flight. Spine item 4's write half — `session_events`, migration `0052`, written inside `apply_frame`                           |
| The transcript reads calls from rows            | #4180 | In flight, stacked on #4179. `rollout_calls` deleted — the last Claude frame parser on the read path                              |

Everything the previous month planned and landed is in `git log` and in the PRs; this document
does not re-list it. What it does assume, and what a reader should check before trusting the
ordering below: the outbox is durable and record-first, turn state is on the turn row, provenance
columns exist on `session_messages` with the ordering half of their `CHECK`, a stored tool call is
a `RecordedToolCall` rather than a Claude content block, and `x/` is split into the runtime,
`channels/matrix/` and `claude_code/`.

**The drift audit is discharged into this document.**
[The drift audit](../console/debug/2026_08_16_plan_code_drift.md) read every chat-runtime plan
against the code and found nineteen divergences. Its § Discharged maps each one: most closed on
`devel`, and the rest are items here rather than a separate list — row 3's provenance `CHECK` and
row 4's backfill are both inside item 4, row 9 is item C, row 16 is in "Not this month" with the
reason, and row 17 is superseded by the splits that have since landed. **That audit is now a dated snapshot, not a
worklist**; a fresh one is worth running when plan and code have drifted again, but re-deriving
those rows would only produce a second table saying what this one says.

## Settle this before writing any of the spine

**Where does the cursor live, and where does the reducer's state at the cursor come from?**

<chat_runtime_projection.md> § The shape specifies "the cursor advances in the same transaction as
its effects" and says nothing about the second half, because when it was written the projection
had no state to speak of. A reducer does: `reduce(state, frames) -> (state, updates)` is only
resumable if the `state` at the cursor is recoverable. Three candidate answers, and the cost
differs by an order of magnitude:

1. **Persist the state beside the cursor**, as a column. Exact and expensive to evolve — the
   state's shape becomes a wire contract across a `maxUnavailable: 0` roll, which is the
   expand/contract discipline <../console/x/README.md> § the wake channel already describes.
2. **Put the cursor only where the state is empty** — after an explicit `finish(state)`. Cheap
   and correct, and coarse: a turn interrupted halfway re-projects from its start.
3. **Recompute the state by re-projecting from `session_turns.first_frame_seq`**, which already
   records where a turn's frames begin and is already how `adopt_open_turn` bounds its questions.
   Bounded by one turn rather than one session, and needs no new durable shape at all.

(3) looks right and is not obviously right: the census (<../console/debug/frame_shape_census.md>)
records how heavy the heaviest sessions are, and a turn in one of them is not a small
re-projection. **This is the one thing to measure before writing item 3**, not to argue about.

**Measured, and answered (#4178).** The fold costs ≈1 µs/frame — 14 ms for the 14,000-frame
heaviest session, ~1.5 ms for a turn of it. (3) is affordable and was still not needed: the turn
loop seeds a fresh `ProjectionState` per frame, so every frame boundary is already a finish
boundary and (2) costs nothing. (3) becomes the answer the moment the loop threads one state across
a turn; (1) was not attempted, and the roll-safety cost that made it last is unchanged.

## The spine

Ordered because each one is what makes the next expressible. Each says how you would know it is
done.

### 1. The projection becomes a reducer — **landed** (#4149)

`project(frames) -> Projection` is stateless over a whole sequence: re-runnable, not resumable.
`project(state, frames) -> (state, Projection)` with an explicit `finish(state)` is the shape
<chat_runtime_projection.md> § The shape specified all along.

**Done when** reducing a frame sequence in one batch equals reducing it in any split of batches,
asserted as a test over a real session's frames — because that equality is exactly the claim that
"project each frame as it lands" and "project from the stored cursor, which happens to be behind"
are one code path.

**Deliberately not in it:** the cursor. This item changes a function's shape and stores nothing,
which is what makes it reviewable by reading it — the same cut #4134 made and for the same
reason.

### 2. A frame at the console's boundary carries a number — **landed** (#4164, #4166, #4172)

`ReceivedFrame.frame_seq` is `int | None`, so #4134 fabricates `_UNNUMBERED_FRAME = -1` to project
at all, and holds "it never reaches a row" as a convention in a docstring rather than as a type.
#4143 concluded the same seam from the other end: the provenance requirement is not expressible
while a frame's sequence may be absent.

The console constructs its only client with a `RolloutRecorder` unconditionally, at one site with
no branch, so an unnumbered frame is not a state production reaches. A numbered type at that
boundary is what makes the invariant checkable instead of narrated.

**Done when** `_UNNUMBERED_FRAME` is deleted and no code path can hand the projection a frame
whose sequence is `None`.

**Why here:** item 4 cannot express its `CHECK` without it, and item 3 would otherwise carry the
placeholder into a durable position.

### 3. The durable cursor — **landed** (#4178)

A per-session position that advances in the same transaction as the effects the reducer produced.
This is the item the whole month is arranged around, and it is what deletes code rather than
adding it: `adopt_open_turn`'s three-way case analysis (never asked / finished-unrecorded /
still-running) is the second state machine, and it exists only because there is no position to
resume from.

**Done when** the live path and the adoption path call the same function with a different starting
cursor, and `adopt_open_turn` no longer asks the frames which of three situations a turn is in.

`sessions.projected_frame_seq` (migration `0051`), advanced inside `SessionStore.apply_frame`
alongside the message row, the outbox row and the turn's state. Adoption returns the frames past
the cursor and the turn loop replays them through the same call the socket feeds, so the two paths
are one.

**Two things it found that this plan had wrong.** `_prompt_left` is _not_ the cursor's to take: it
asks whether the console's own **outbound** write happened, which the fold projects to nothing by
design — so two of the three cases collapsed and the third is a different kind of question, still
asked of the frames. And the cursor **cannot be backfilled at all**: no query can say how far a
previous holder got, since `max(frame_seq)` loses a turn's ending and `0` re-projects committed
effects. NULL therefore means "no cursor" and the pre-cursor path stays behind a tombstone gated on
`session_ttl_seconds`, which is a qualification of "the three-way case analysis goes" rather than
the clean deletion this section promised.

**The state-at-the-cursor question was measured** at ≈1 µs/frame, so option (3) — re-projecting
from `first_frame_seq` — costs ~1.5 ms for a heavy turn and 14 ms for the 14,000-frame worst case.
It is affordable, and was still not needed: the loop seeds a fresh `ProjectionState` per frame, so
every frame boundary is a finish boundary and option (2) is free. (3) is the answer the moment the
loop threads one state across a turn.

**Note what it does not need:** persisted events. The effects it advances beside are the rows that
already exist — the message upsert, the turn open and close, the outbox row. One column, no rows
rewritten. That is deliberate; see item 4.

**The alternative order, stated so the choice is visible:** store the events first and the cursor
becomes derivable (the greatest `frame_seq` among a session's stored events) rather than a column
of its own. That is tidier and it front-loads the migration onto a shape that has not yet been
proven resumable. Cursor first, on the review-attention argument.

### 4. The neutral events, stored — **landed** (#4179, #4180)

The table <../console/plans/session_channels.md> § 3 wants for lifecycle, the table
<chat_runtime_cleanup.md> § stage 7 calls a `tool_call` table, and the table #4143 says the
provenance requirement has to move to are **one table**. This plan makes that explicit, because
the two design docs currently name two:

> **This paragraph's premise is wrong on one of the three, and the correction matters.**
> `session_channels.md` § 3 does not ask for a table — it decided against one: _"lifecycle events
> become frame-log rows under their own bridge-side `kind`, **not a new table** — which also makes
> § 1's cursor a position in one ordered log rather than a join across several."_ So it was two
> documents asking for a table and one that had ruled it out. The consequence is live: `session_events`
> ships with **no lifecycle writer, and therefore no writer for the `authored` provenance arm**. The
> arm is still right — it is what makes the `CHECK` expressible, and adding it later would be a
> second migration on this table — but whoever builds the session-event category has to reconcile
> § 3's frame-log decision with stage 4's "one ordered stream" first.

- **Against a separate `tool_call` table.** <chat_runtime_projection.md> § stage 4 settled that
  tool calls live in the neutral layer as a **lifecycle** — `ToolCallStarted` → `ToolCallCompleted`
  — because Matrix already displays calls in progress and that display has to come from somewhere
  neutral. A `tool_call` table holds finished records and cannot express the in-flight state, so
  building it would be a second answer to a question stage 4 already answered. `chat_runtime_cleanup.md`
  § stage 7's second bullet should be struck in favour of this.
- **The result goes in with it.** #4140 stored the call and argued the result stays joined at read
  time out of the rollout. That was right for a change whose thesis was that a stored call stops
  being a Claude content block; it is not a resting place, because the join is `rollout_calls` and
  `rollout_calls` is a Claude frame parser.
- **Provenance is a union here, and that is why the `CHECK` fits.** `frame_range | authored` is a
  real discriminator on an event row where on `session_messages` it was three meanings of `NULL`
  (#4143).

**Done when** `session_views.rollout_calls` is deleted, a tool call's result is read from a row,
and a new event row cannot be written without a provenance union.

**The backfill is the reprojection tool**, not archaeology — project a session's frames, align
against the stored rows, write the range where the alignment is unambiguous, and treat an
ambiguous one as a finding about the projection. That tool is also the drift check
<chat_runtime_projection.md> § "What makes it safe" asks for, and it should be written once and
used for both.

**Still owed, and larger than it reads here.** #4179 and #4180 built the table and moved the
readers; the tool is a third PR, for three reasons the paragraph above did not anticipate:

- **`project_log` over a session is the wrong fold to compare against.** The write path folds
  **per frame, seeded empty, under `DeltaSource.STREAM_EVENTS`**; `project_log` over a whole
  session merges frames sharing one `message.id` and cuts deltas from completed blocks. Both are
  correct and they are different event sequences, so a checker using the second would report drift
  everywhere. It has to reproduce the fold _as the write path configures it_, which means
  `session_runtime._projected` stops being private and becomes the one function writer and checker
  share. That refactor is the tool's first commit.
- **The check has an era bound**, the same shape as the cursor's in item 3: a turn served by a
  replica on the previous image has frames and no event rows, which is indistinguishable from a
  projection that has stopped producing. It must run per turn, skip a turn with no rows at all,
  and say so — or it reports drift on every live session for one `session_ttl_seconds` after the
  release.
- **`session_events` has nothing to backfill.** Rows cannot be written retroactively, because the
  cursor that makes them exactly-once did not exist then. What the backfill paragraph is actually
  describing is `session_messages.source_{first,last}_frame_seq` for rows with no agent id — a
  different target from this table.

**Two vocabulary members deliberately get no row**, so the stored stream is not the projected
stream: `TextDelta` (an increment of prose `MessageCompleted` carries whole, hundreds per turn —
the `stream_event` frames stay the evidence) and `TurnCompleted` (outcome, the neutral usage
columns from #4169 and the frame bracket are all `session_turns`). A reader wanting a session's
events in order unions the table with `session_turns`.

## In parallel

Nothing in the spine blocks these, so they are dispatched rather than queued.

### A. The status line stops reading Claude's frames

`room_status.coarse_status` matches on `assistant` and `system`/`task_started` from inside the
channel layer. #4134 names it as the one thing that would make a **second backend go quiet while
the agent works**, and it is the reason the SPA has no in-progress display at all. The turn loop
already projects every frame it holds, so the events the status line needs —
`ToolCallStarted(name)`, `ActivityStarted(description)`, text arriving — are in hand at the call
site.

**Done when** `room_status.py` contains no frame `type` string and imports nothing from
`session_frames.py`.

**Do this first among the parallel items.** It is the cheapest of the four and it is the one whose
absence is a live product gap rather than a structural one.

### B. A turn's cost stops being one CLI's payload

`end_turn` stores Claude's `result` frame verbatim as the record of cost, usage and duration, so
"this turn cost X" quietly means "whatever that one CLI reported". `TurnCompleted.usage` is the
neutral shape that replaces it, and <chat_runtime_projection.md> § "Does a turn live over frames
or over neutral events" adds one requirement worth honouring while the schema is being written:
the neutral usage must be **aggregatable**, because an exchange may one day sum several
invocations.

**Done when** cost, usage and duration are columns whose meaning does not depend on which backend
produced them, and the raw payload is kept as evidence rather than read as the answer.

### C. One sessions surface, and the SSE stream retires

`/chat` and `/conversations` are the same object behind two routes, two nav entries and two data
paths. Merging them is what makes `/api/sessions/{session_id}/stream` and `claude_chat_page.tsx`
deletable (<../console/plans/one_read_api.md> § Stage 3, <../console/plans/session_channels.md>
§ 2), and retiring the SSE stream is what makes the `asyncio.wait` abort dance in the turn loop
removable.

**Done when** `claude_chat_page.tsx` is deleted, the SSE route is unmounted, and the console has
one live-update mechanism.

**This is the item to cut if the month runs short.** It is the only one that is features rather
than structure, it depends on nothing else here, and the regression it takes knowingly — near-live
refetch instead of per-token streaming — is a judgment nobody has made yet against the real page.

### D. The directory finish — after the spine, not beside it

Mostly executed while this plan was being written: `claude_projection.py` became
`claude_code/projection.py` (#4154) and the runtime-side mirror landed as #4141. What is left is
the one file that move deliberately would not split — `session_frames.py`, whose four frame-kind
constants are the CLI's own `type` values while `SETUP_OUTPUT_KIND` is the bridge envelope and the
console's own authored row. Its own TODO already says these are not one vocabulary.

**Done when** no module at `x/`'s top level is named for or specific to one CLI harness.

**Sequenced after item 4** for the reason the previous plan already learned: moving a file while it
is being rewritten is the worst version of a move, and items 1–4 rewrite exactly these files. Two
hazards the previous plan recorded that a mechanical rename still does not catch: a dotted-string
`patch("haku.console.x.…")` is a string, so a moved symbol makes it fail at call time or, worse,
patch nothing; and `bb run //devinfra:gazelle` is unavailable in agent sessions (403 from the
egress proxy on `rules_mypy`), so every `BUILD.bazel` `deps` edit is by hand and wants a
`bbr build` on the named library to prove it.

## The rule the outbox was a special case of

**Operator, 2026-08-16:** _no events should be written directly into Matrix without going through
our database, because Matrix is just one of pluggable backends — channels._

So the outbox is not a fix for one bug, it is the shape every outbound channel write has to take:
**recorded first, sent from the record.** A write that goes straight to the homeserver is invisible
to every other channel, unrecoverable across a crash, and unprojectable — which is the whole class
the drop audit was chasing, arrived at from the other direction.

The test of compliance is not "does it work" but "could Telegram show it": anything only Matrix can
do is a gap even while it works, because the second channel is what the neutral layer exists for.
That makes the easy-to-forget writes the interesting ones — typing indicators, edits, redactions,
invites, and the console's own notices ("holding N messages", abort notices, error reports) — none
of which look like messages and all of which are things a reader of a second channel would miss.
<../console/debug/channel_write_audit.md> is the inventory.

A standing constraint rather than a scheduled item: it governs A and C above, and anything else
this month that writes outward.

## The divergence this plan found

**Two folds now answer "what tool calls did this message make", and they can disagree.**

`GET /api/conversations/{session_id}` assembles its transcript through `session_views.rollout_calls`
— a Claude frame parser, re-deriving calls and results on every request. `haku_conversations.read_transcript`
folds the same session through `claude_projection` (#4145). Same rows, two interpreters, two
consumers: the operator's SPA reads one and Haku reads the other.

Neither document records this, because each change was locally right. #4145's thesis was that the
agent should read the neutral conversation, and it delivered that; retiring the REST view's own
parser was explicitly somebody else's PR. But the effect is that the interpreter count did not go
down when the turn loop moved onto the projection — the projection gained a reader while the old
readers stayed.

It also doubles a known cost: <chat_runtime_cleanup.md> § Anytime describes the O(session) read on
the SPA path, and the MCP fold pays the same order per page, by design and for a stated reason
(a suffix is not deterministic).

**The position:** this is item 4's job and item 4 is where it should be closed — but if item 4
slips, pointing the REST detail view at the same fold is a smaller change than waiting, and it is
worth pricing rather than assuming. What must not happen is the month ending with both still
live.

## The second backend: what is actually in the way

The neutral vocabulary exists for this, so it is worth being exact about the remainder rather than
carrying "a second backend" as a permanent aspiration.

**Code-side, and all of it is scheduled above:** the status line reading Claude's frames (item A),
`end_turn` storing one CLI's payload as the record of cost (item B), and the failure reason being
quoted from the frame (item B's neighbour). #4134 lists exactly these three and nothing else about
the turn loop, which is the evidence that the loop itself is now neutral.

**Not scheduled, and deliberately:** the control channel — `ClaudeCli` owns `initialize` and
`interrupt` in Claude's `control_request`/`control_response` spelling — and choosing a backend per
session, which the console does not do because it imports `build_claude_launch` statically. Both
are seams with one caller, and <../runtime/x/claude_bridge/docs/second_backend.md> is right that
inventing a registry before a second backend exists is a mechanism with one user.

**The real blocker is that there is no second backend to check against.** There is no Codex
credential in this cluster, so every claim in this repo about what a second backend would need is
read from documentation rather than measured — which that document says about itself, honestly,
and which no amount of further design changes.

**A cheap way to make it measurable, proposed here rather than found in a plan:** the tests
already run a stub CLI as a real process (`claude_code/testing/stub_claude.py`, a `py_binary` the
runner execs). A **second stub speaking a deliberately different frame vocabulary** — different
type names, deltas spelled differently, its own result shape — would exercise `CliBackend`,
`replayable`, a second adapter into `conversation_events` and the status line end to end, with no
credential and no vendor. It would answer "is the seam real" the way the Synapse container
answered "is the sync loop real". Not scheduled this month; costed here so the choice is
available.

## Not this month, and why

A plan that includes everything schedules nothing. Each of these is real work with a reason it is
not now.

- **Streaming increments to the frontend instead of refetching.** The operator's stated direction
  (<../console/plans/session_channels.md> § 4), and it is a **consequence** of the spine rather
  than an item beside it: the increment has to be named in a stored event stream and addressed by
  a per-consumer position, which are items 4 and 3. Invalidate-then-refetch is the honest
  implementation until they exist, and it is idempotent, which is the property that makes a missed
  event harmless. Scheduling it earlier would mean inventing a payload shape twice.
- **The reconcile loop and `chat_attachment`** (<../console/plans/session_channels.md> § 1). The
  outbox is the push half and it works; the convergence half wants cleanup stage 7's schema and
  pays off with a **second** channel or the Matrix relay, neither of which is this month. Pull it
  in the moment either becomes real.
- **Projection stage 2** — the frame table's two `kind` vocabularies. Three releases, nothing in
  the spine depends on it, and it is the same judgment the previous plan made.
- **Stage 6, allocate on demand**, and two rooms on one bot account. A standing cost rather than a
  defect: one sandbox held for a quiet room, on a quota that is not currently binding. It is also
  a two-release enum widening. First thing to pull in if the month runs ahead.
- **Multi-agent trust tiers** (<information_trust_tiers.md>). Deferred again, on the same
  reasoning as last month: the month should end with a structure to build on rather than a new
  subsystem standing on a half-built one. Item 4 is what it wants, and it starts cheaply after.
- **Mid-turn steering.** It is the change that splits the turn's three jobs — mutex, recovery
  marker, accounting unit. Doing it before the neutral turn owns its own boundaries would split
  them in the wrong layer.
- **Promoting the provenance `CHECK` on `session_messages` to `VALIDATE`**, and any decision about
  dropping history to get it. `session_messages` is the `haku_index` chat corpus, so dropping it
  deletes Haku's memory; that is a deliberate decision if it is ever made, not a side effect of
  wanting a `NOT NULL`.
- **Sourcing the CLI from npm** (<cli_protocol_ownership.md>). Unrelated to any of the above and
  independently dispatchable whenever someone wants it.

**Small enough to slot into any week with room**, listed so they are not forgotten: slash commands
and the room's session link (both wait on C for the route), `bridge_token_fingerprint = b""` →
`claim_cleaned_at`, and the archaeology prune in `session_{runtime,store}.py`.

## What is uncertain

Named rather than guessed, because a wrong confident answer here costs a migration.

- **Whether re-projecting from a turn boundary is cheap enough** to be the cursor's state answer.
  Measurable today against a census-heavy session; nobody has measured it.
- **Whether `Projection.unprojected` is small in production.** The projection counts the frame
  classes this release has no meaning for, precisely because the CLI keeps adding them, and the
  count has not been read off real sessions since the census. Item 4 stores what the projection
  produces, so a large `unprojected` is a reason to delay it rather than a detail.
- **Whether a coalesced refetch actually feels as good as the SSE stream.** Item C takes that
  regression knowingly and it has never been compared on the real page by a person.
- **Whether the neutral turn's usage shape can be settled before a second backend exists.** Item B
  designs an aggregatable shape from one example, which is the failure mode the whole
  neutral-vocabulary exercise exists to avoid.
- **Whether "a second backend works" is worth any more code before something can check it.** The
  stub-harness proposal above is the cheapest test of that question and it may still not be worth
  its cost.

## Being honest about the size

The spine is four items and three of them touch the hot path in production. The parallel lane is
four more. That is a full month if nothing goes wrong, and something will.

**The order is chosen so that slipping is safe.** Item 1 is useful alone (a resumable projection
with no consumer is still a better projection). Item 2 is useful alone (a deleted placeholder and
a type that says what production already guarantees). Item 3 deletes the second state machine and
is the month's payoff even if item 4 never starts. Items A and B are independently shippable and
each closes a named second-backend blocker.

**If something has to give, give C.** It is features, it blocks nothing, and it is the only item
whose value is a matter of taste.

**The one not to cut is item 2.** It is the smallest thing here and it is the seam two separate
investigations (#4134 and #4143) arrived at independently from opposite ends. Skipping it means
item 4 writes a constraint around a placeholder, which is the shape of mistake that survives for
a year.
