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

## The thesis

**The projection exists and nothing is stored through it.** `claude_projection.py` is the one
interpreter that four bodies of code were supposed to collapse into, `conversation_events.py` is
the vocabulary, the turn loop drives its live path off both (#4134), and the MCP transcript reads
through them (#4145) — but every row the system keeps is still written by the code that was there
before, and every recovery path still reconstructs state from Claude's frames.

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

| What                                            | Where                             | Note                                                                                                                |
| ----------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| The projection becomes a reducer                | `claude/haku-projection-reducer2` | Spine item 1. No PR yet at the time of writing                                                                      |
| `claude_bridge/` → `bridge/`                    | #4141                             | The runtime half of the directory finish; independent of everything else here                                       |
| Block style where Flux rewrites, plus the guard | #4147                             | Not chat-runtime work, and it matters anyway: it is what stops unrelated PRs going red on a file they never touched |

Everything the previous month planned and landed is in `git log` and in the PRs; this document
does not re-list it. What it does assume, and what a reader should check before trusting the
ordering below: the outbox is durable and record-first, turn state is on the turn row, provenance
columns exist on `session_messages` with the ordering half of their `CHECK`, a stored tool call is
a `RecordedToolCall` rather than a Claude content block, and `x/` is split into the runtime,
`channels/matrix/` and `claude_code/`.

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

## The spine

Ordered because each one is what makes the next expressible. Each says how you would know it is
done.

### 1. The projection becomes a reducer — in flight

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

### 2. A frame at the console's boundary carries a number

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

### 3. The durable cursor

A per-session position that advances in the same transaction as the effects the reducer produced.
This is the item the whole month is arranged around, and it is what deletes code rather than
adding it: `adopt_open_turn`'s three-way case analysis (never asked / finished-unrecorded /
still-running) is the second state machine, and it exists only because there is no position to
resume from.

**Done when** the live path and the adoption path call the same function with a different starting
cursor, and `adopt_open_turn` no longer asks the frames which of three situations a turn is in.

**Note what it does not need:** persisted events. The effects it advances beside are the rows that
already exist — the message upsert, the turn open and close, the outbox row. One column, no rows
rewritten. That is deliberate; see item 4.

**The alternative order, stated so the choice is visible:** store the events first and the cursor
becomes derivable (the greatest `frame_seq` among a session's stored events) rather than a column
of its own. That is tidier and it front-loads the migration onto a shape that has not yet been
proven resumable. Cursor first, on the review-attention argument.

### 4. The neutral events, stored

The table `session_channels.md` § 3 wants for lifecycle, the table
<chat_runtime_cleanup.md> § stage 7 calls a `tool_call` table, and the table #4143 says the
provenance requirement has to move to are **one table**. This plan makes that explicit, because
the two design docs currently name two:

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

`claude_code/` holds one file, `testing/stub_claude.py`, on #4139's stated intent that
`claude_projection.py` and the frame adapter land there once the projection work settles. They are
what the directory is named for. The runtime-side mirror is #4141.

**Done when** no module at `x/`'s top level is named for or specific to one CLI harness.

**Sequenced after item 4** for the reason the previous plan already learned: moving a file while it
is being rewritten is the worst version of a move, and items 1–4 rewrite exactly these files.

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
