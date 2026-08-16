# The chat runtime — what is left

**A schedule, not a design.** It orders [the projection plan](chat_runtime_projection.md), [the
cleanup plan](chat_runtime_cleanup.md), [the channels plan](../console/plans/session_channels.md)
and [the read-API plan](../console/plans/one_read_api.md); where it disagrees with one of them it
says so and the design document is what moves. Nothing here restates a design. Delete this file
when the list is empty rather than letting it rot into a second backlog.

## There is no thesis

The spine this document was arranged around — **the projection exists and nothing is stored through
it** — is discharged. The projection is a reducer (#4149), a numbered frame reaches it (#4164,
#4166, #4172), it has a durable position (#4178), and it owns rows (#4179, #4180 — `session_events`,
`rollout_calls` deleted). The parallel lane went with it: the status line reads events (#4162), a
turn's cost is columns (#4169), the frame vocabulary is split (#4181).

What is left is a **list**, not a spine. Nothing here blocks anything else, so it is ordered by what
a delay costs. Dispatch in parallel (`AGENTS.md` § Splitting Work Into PRs).

**Items 2 and 3 are now downstream of one decision.** The operator has accepted dropping the
early chat data outright — two days of it, nothing worth keeping — rather than carrying the branches
and nullable columns that exist to accommodate it. [The purge plan](legacy_purge.md) sequences that:
what dies by migration, what dies by dropping rows, and the constraints that become expressible once
the rows are gone. Where it disagrees with an item below, it is the later document and it wins.

## The list

### 1. Stage 6's enum widening — pulled in, because the calendar is its long pole

[chat_runtime_cleanup.md](chat_runtime_cleanup.md) § Stage 6 allocates a sandbox because there is
something to do, instead of holding one indefinitely for a quiet room. It was named as the first
thing to pull in if the schedule ran ahead. It has.

Its first step is not the behaviour: `TextBackedStrEnumColumn` parses the status column, so a
replica on the previous image reading `idle` fails rather than degrading. The member and the
`CHECK` widen in one release and the first `idle` row is written in the next. **Split it that way
and ship the widening now** — the code is a line and the wait is a roll.

**Done when** an idle room holds no sandbox and the first message provisions one.

### 2. The contract halves that are now due

`devel` carries expand/contract tombstones whose gate is a roll converging, each with the query that
answers it beside the thing it guards. One is this work's own residue: the pre-cursor adoption
branch with `_recorded_completion` and `RESULT_FRAME_KIND` (#4178's, gated on `session_ttl_seconds`).
The rest are `_write_partial_frame` and its `partial` column. `session_turns.usage` (#4169's) and
`session_messages.tool_uses` are done — `0056` dropped both.

**Done when** `rg 'CLEANUP\(added' haku/console` names only tombstones whose gate query does not yet
return clear.

**Second because it is hours of work rather than days**, and because a gate that has cleared does
not un-clear.

### 3. The session-event category — blocked on a decision, not on work

`session_events.provenance` has an `authored` arm and **no writer**, because two design documents
disagree and both are on `devel`:

- [chat_runtime_projection.md](chat_runtime_projection.md) § stage 4 wants one ordered stream in two
  categories — conversation, and what happened _to_ the session. The `authored` arm exists to carry
  the second, which crosses no wire.
- [session_channels.md](../console/plans/session_channels.md) § 3 ruled a table out: lifecycle
  events become frame-log rows under their own bridge-side `kind`, "**not a new table** — which also
  makes § 1's cursor a position in one ordered log rather than a join across several".

Neither is obviously wrong. § 3's argument still bites: a session that died before the CLI produced
anything has its whole story in the frame log, and `session_events` holds nothing for a session that
never reached a turn — `turn_id` is `NOT NULL`, so the second category needs a migration whichever
way the argument goes. § 4's argument is that a lease changing hands is a console-side fact with no
frame and never will have one, so a frame-log row for it is an envelope invented to fit.

**This is the one item that cannot be specified until it is decided**, which is the only legitimate
reason to hold work (`AGENTS.md` § Splitting Work Into PRs). Everything else here is dispatchable
today.

**Done when** one of the two documents says the other is wrong, and `EventProvenance.AUTHORED` has a
writer or is deleted. An arm with no writer is a column with no reader (`STYLE.md` § General), and
it should not survive a second release in that state.

### C. One sessions surface, and the SSE stream — the operator's call

`/chat` and `/conversations` are the same object behind two routes, two nav entries and two data
paths. Merging them deletes `claude_chat_page.tsx`, unmounts `/api/sessions/{session_id}/stream`,
and makes the `asyncio.wait` abort dance in `_run_turn` removable
([session_channels.md](../console/plans/session_channels.md) § 2, [the read-API
plan](../console/plans/one_read_api.md) § Stage 3).

**Done when** `claude_chat_page.tsx` is deleted, the SSE route is unmounted, and the console has one
live-update mechanism.

**The decision, framed rather than made.** The merge trades per-token streaming for near-live
refetch, and nobody has compared the two on the real page. The trade need not be permanent: § Not
now's "stream the increment" was blocked on an ordered neutral event stream and an address within
it, and `session_events` with its `event_seq` is both — only the per-consumer position is still
missing. So the question is whether refetch is good enough for the releases between the merge and
the increment, not whether it is good enough forever. Three shapes:

- **Merge now, take the regression, build the increment after.** One live path immediately; the page
  is worse for however long the increment takes.
- **Build the increment first and merge onto it.** No regression at any point, at the cost of
  designing it against a page that is about to be replaced and keeping `/chat` alive meanwhile.
- **Neither.** C blocks nothing and is the only item here that is features rather than structure.

**If something has to give, give C.** Nothing on this list depends on it, and neither does the
increment it is judged against.

## The rule the outbox was a special case of

**Operator, 2026-08-16:** _no events should be written directly into Matrix without going through
our database, because Matrix is just one of pluggable backends — channels._

Every outbound channel write is **recorded first, sent from the record**. A write that goes straight
to the homeserver is invisible to every other channel, unrecoverable across a crash, and
unprojectable. The test of compliance is not "does it work" but "could Telegram show it" — which
makes the easy-to-forget writes the interesting ones: typing indicators, edits, redactions, invites,
and the console's own notices. [The channel write
audit](../console/debug/channel_write_audit.md) is the inventory.

A standing constraint rather than a scheduled item. It governs C and anything else that writes
outward.

## The second backend: what is actually in the way

**Nothing on this list.** Of the three blockers #4134 named, the status line and `end_turn`'s
payload closed with items A and B; the third — a turn's failure reason quoted from the frame — is
settled rather than open, because _why_ a turn failed is provider-specific and the neutral
vocabulary carries an outcome instead. What remains is the control channel (`ClaudeCli` owns
`initialize` and `interrupt` in Claude's `control_request`/`control_response` spelling) and choosing
a backend per session, which the console cannot do while `session_runtime.py` imports
`build_claude_launch` statically. Both are seams with one caller, and [the second-backend
note](../runtime/x/bridge/docs/second_backend.md) is right that a registry before a second backend
exists is a mechanism with one user.

**The real blocker is that there is nothing to check against.** No Codex credential exists in this
cluster, so every claim in this repo about a second backend is read from documentation rather than
measured. The cheapest fix: the tests already run a stub CLI as a real process
(`claude_code/testing/stub_claude.py`, a `py_binary` the runner execs), and a **second stub speaking
a deliberately different frame vocabulary** would exercise `CliBackend`, `replayable`, a second
adapter into `conversation_events` and the status line end to end, with no credential and no vendor.
Costed, not scheduled.

## Not now

- **Stream the increment to the frontend** ([session_channels.md](../console/plans/session_channels.md)
  § 4). Its two preconditions are half met: the ordered stream and its address
  exist, the per-consumer position does not. It is the thing item C should be judged against; see
  there.
- **Projection stage 2 — the frame table's two `kind` vocabularies**, and with it releases R2–R5 of
  the numbering schedule ([chat_runtime_projection.md](chat_runtime_projection.md) § 2b). R1 landed;
  the rest are four release-gated steps and nothing here depends on them. This is also where item
  D's residue goes: `session_store.py` and `session_runtime.py` still _speak_ the CLI's vocabulary,
  which is a column and an adapter port rather than a move, recorded in
  [the runtime README](../console/x/README.md).
- **Threading one projection state across a turn.** The cursor rests on per-frame seeding, so every
  frame boundary is a finish boundary; threading makes re-projection from
  `session_turns.first_frame_seq` the answer instead, measured at ≈1 µs/frame and affordable. Two
  loop-side bugs block it and `session_runtime._projected` names both. Pull it in when a message
  spanning frames needs to be one row.
- **The reconcile loop and `chat_attachment`**
  ([session_channels.md](../console/plans/session_channels.md) § 1). The outbox is the push half and
  it works; the convergence half pays off with a **second** channel or the Matrix relay. Pull it in
  the moment either becomes real.
- **Multi-agent trust tiers** ([the trust-tier plan](information_trust_tiers.md)). The message
  provenance it wants exists now (`source_{first,last}_frame_seq`, recovered by #4191), so it starts
  cheaply — but it is a new subsystem, and the list above is what makes it stand on something
  finished.
- **Mid-turn steering**, which splits the turn's three jobs — mutex, recovery marker, accounting
  unit. Doing it before the neutral turn owns its own boundaries splits them in the wrong layer.
- **Promoting the provenance `CHECK` on `session_messages` to `VALIDATE`**, and any decision about
  dropping history to get it. `session_messages` is the `haku_index` chat corpus, so dropping it
  deletes Haku's memory.
- **Sourcing the CLI from npm** ([the protocol-ownership plan](cli_protocol_ownership.md)).
  Unrelated to all of the above and independently dispatchable whenever someone wants it.

**Small enough to slot into any week with room:** slash commands and the room's session link, both
of which want C's route settled first because a posted Matrix event is permanent and federated.

## What is uncertain

- **Whether a coalesced refetch feels as good as the SSE stream.** Item C takes that regression
  knowingly and nobody has compared them on the real page.
- **Whether the neutral turn's usage shape survives a second backend.** #4169 designed an
  aggregatable shape from one example, which is the failure mode the neutral vocabulary exists to
  avoid, and nothing can check it until there is a second backend or a second stub.
- **Whether "a second backend works" is worth more code before anything can check it.** The stub
  harness is the cheapest test of that question and may still not be worth its cost.
