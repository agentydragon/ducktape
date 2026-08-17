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

## The list

### 1. Finish the legacy purge

The operator accepted dropping the early chat data outright rather than carrying the branches and
nullable columns that accommodated it:

> you messed up the first implementation, but I only used it for like 2 days and for nothing serious
> that I'd mind losing. I'm happy to drop old data including the derived index if it helps us make
> the code clean and correct going forward. I expect it should be possible to have constraints that
> actually express the requirements we will have met in production data once we get all the old
> legacy out.

**The rows are gone.** Phase 1 ran on 2026-08-16: 302 sessions deleted, 35,795 frames and 2,506
messages with them, the sandbox cycled, a replacement session provisioned. What it deleted, what made
it safe, and which gates it cleared are in
<../console/debug/2026_08_16_legacy_purge.md> — including the correction that the frame log is
**not** permanently empty, because `frame_seq` is still `Identity(always=True)` until the numbering
cutover.

What is left is three releases.

#### The ordering constraints, stated once

1. **The console rolls `maxUnavailable: 0` with migrations at startup**, so a replica on the previous
   image runs against the new schema for the length of the roll. SQLAlchemy names every _mapped_
   column in every `SELECT`, so **every `DROP COLUMN` is two releases**: one that unmaps and deletes
   the readers, one that drops.
2. **Adding a `CHECK` needs no split** when the tables are empty and the previous image's writers
   already satisfy it — true after phase 1, verified per constraint below. So the tightening rides
   along with the code-only release rather than waiting for a third.
3. **`projected_frame_seq` does _not_ need a split**, though it looks like it should. The previous
   image can insert a session with no cursor — but `create()` never sets the attribute and it has no
   Python default, so SQLAlchemy omits the column from the `INSERT` entirely and the new server
   default applies. `SET DEFAULT 0` and `SET NOT NULL` can therefore land together. (Observing a
   fresh session at `NULL` beforehand does not settle this: pre-default, an omitted column and an
   explicit `NULL` are indistinguishable in the row. The ORM's emit behaviour is what settles it.)

#### Phase 2 — the code-only release, plus the additive tightening

Unmap `session_messages.{unpointable_reason,tool_calls}` and `session_frames.partial` and delete
every reader; delete `message_view`'s `recorded or message.tool_calls` fallback; and one additive
migration: `sessions.surface SET NOT NULL`, the surface/room equivalence,
`VALIDATE CONSTRAINT ck_session_messages_source_anchored`, `ck_session_messages_assistant_pointed`,
`ck_session_frames_runner_seq_direction`, and `projected_frame_seq` `SET DEFAULT 0` **and**
`SET NOT NULL`.

**`ck_session_frames_wire_numbered` is not in phase 2**, and the reason is a live writer rather than
old data: `_write_partial_frame` writes a `from_agent`/`assistant` row carrying no runner number on
every stream delta, in the serving image and the next one alike. Adding the check would break
streaming the moment it landed. It goes in phase 3, after the writer is deleted and its rows with
it.

Each addition is safe against the previous image for a specific reason, not a general one:

| Addition                                       | Why the previous image cannot violate it                                                                                                                              |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `surface NOT NULL`                             | `SessionStore.create` always sets `surface` from the variant's `surface_column`                                                                                       |
| `(surface = 'matrix') = (room_id IS NOT NULL)` | Same call site sets `room_id` from the same variant                                                                                                                   |
| `ck_session_messages_assistant_pointed`        | Both `_open_assistant` call sites pass a `frame_seq`; there is no third writer                                                                                        |
| `VALIDATE … source_anchored`                   | The table is empty, and `update_assistant` only widens a range `begin_assistant` set                                                                                  |
| `ck_session_frames_wire_numbered`              | Holds only because phase 1 cycled the runner — **verify the gate query first**                                                                                        |
| `projected_frame_seq SET DEFAULT 0`            | `create()` never sets the attribute, so the ORM omits it and the default applies — **measured** on the replacement session, which came up `NULL` under the old schema |

Prove each before writing it. Every query returns 0:

```sql
SELECT count(*) FROM sessions WHERE surface IS NULL;
SELECT count(*) FROM sessions WHERE (surface = 'matrix') <> (room_id IS NOT NULL);
SELECT count(*) FROM session_messages WHERE role = 'assistant' AND source_first_frame_seq IS NULL;
SELECT count(*) FROM session_messages
  WHERE source_last_frame_seq IS NOT NULL AND source_first_frame_seq IS NULL;
SELECT count(*) FROM session_frames WHERE runner_seq IS NOT NULL AND direction <> 'from_agent';
```

#### Phase 3 — the drop release

Once phase 2 has converged: the remaining `DROP COLUMN`s, `DROP INDEX uq_session_frames_partial`, the
two `unpointable_*` constraints, `DELETE FROM session_frames WHERE partial`, and then
`ck_session_frames_wire_numbered` — which needs those rows gone, not just the writer. Afterwards:

```sql
SELECT column_name FROM information_schema.columns
 WHERE table_name IN ('sessions','session_messages','session_turns','session_frames')
   AND column_name IN ('unpointable_reason','tool_calls','partial');
```

Zero rows, and `ck_session_messages_source_anchored` must be `convalidated = true`.

#### Phase 4 — squash the chain, and the tests that exist only to guard it

The purge's own reward, asked for by the operator: _"once we've migrated prod to proper schema shape
and constraints without weird legacy or wrong data I'd want to drop the load of keeping around all
the migration tests."_ Last, because a chain can only be squashed once the only database that will
ever replay it is stamped past the end of it — **gate:** production stamped at phase 3's head with
every replica on an image at or after it.

**A squash here is not a new technique.** `0010` is one, and its docstring records how: the revision
id of the deployed head is retained, so a database already stamped at it is a no-op while a fresh
database creates the frozen schema directly.

The six dedicated migration tests split three ways, and the split is the point — "drop the migration
tests" would otherwise delete two things that are not about migration at all:

| Test                                         | Rev    | After the squash                                                                                                                                                                            |
| -------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_message_tool_calls_migration.py`       | `0047` | **Goes.** Already moot — `0056` dropped the column it backfills from                                                                                                                        |
| `test_neutral_turn_usage_migration.py`       | `0049` | **Goes.** Same: `0056` dropped `session_turns.usage`                                                                                                                                        |
| `test_session_claim_cleaned_at_migration.py` | `0048` | **Goes.** A backfill of rows phase 1 deleted                                                                                                                                                |
| `test_frame_runner_seq_migration.py`         | `0050` | **Goes.** Asserts a nullable column an old writer could omit; phase 2 makes it not-nullable                                                                                                 |
| `test_session_idle_status_migration.py`      | `0054` | **Becomes a constraint test** — both assertions are about what `ck_sessions_status` admits. Not before item 2 ships, since until then the widening is the live half of a two-release change |
| `test_state_index_migration.py`              | `0037` | **Stays, rebased.** Not a migration test: it compares <../state_index/schema.py> against what the deployed database gets, and nothing else does                                             |

That is 473 of the six files' 624 lines, and no coverage lost — the two tests that assert the
property a squash actually endangers already exist in `test_agent_authority_schema.py`:
`test_fresh_baseline_matches_sqlalchemy_metadata` and `test_database_already_at_head_is_unchanged`.
Those are what <../../STYLE.md> § Testing asks for in place of per-migration change-detectors, and
both must pass **before** the squash lands as well as after — a baseline disagreeing with the ORM is
a console that cannot boot.

**What the squash does not buy.** Every migration written after it is a migration again, so this
collects a debt once rather than changing policy.

#### Deliberately out of scope

- **`session_messages.agent_message_id` stays nullable.** Not a legacy accommodation: a synthesised
  assistant row (text that arrived only on the `result` frame) legitimately has none, and a second
  backend need not supply one. The purge removed the _population_, not the case. Retiring the column
  belongs to <chat_runtime_cleanup.md> § The backend seam.
- **`capabilities.py`** is legacy in a different sense (the haku-ui launch migration), with its own
  tombstone and gate.
- **Promoting anything on `session_frames.kind`.** That two-vocabulary defect is
  <chat_runtime_projection.md> § 2's; dropping `partial` removed the **third** meaning and nothing
  more.

### 2. `SessionStatus.IDLE`'s writer

[chat_runtime_cleanup.md](chat_runtime_cleanup.md) § Stage 6 allocates a sandbox because there is
something to do, instead of holding one indefinitely for a quiet room. The widening shipped a release
early (#4190) because `TextBackedStrEnumColumn` parses the column, so a replica on the previous image
reading `idle` fails rather than degrading; the writer is the second half.

**Done when** an idle room holds no sandbox and the first message provisions one.

### C. One sessions surface, and the SSE stream

`/chat` and `/conversations` are the same object behind two routes, two nav entries and two data
paths. Merging them deletes `claude_chat_page.tsx`, unmounts `/api/sessions/{session_id}/stream`, and
makes the `asyncio.wait` abort dance in `_run_turn` removable
([session_channels.md](../console/plans/session_channels.md) § 2, [the read-API
plan](../console/plans/one_read_api.md) § Stage 3).

**The operator chose: build the increment first, then merge onto it** — no regression at any point,
rather than taking the refetch regression for however long the increment takes. The design is in
`session_channels.md` § 4.

**Done when** `claude_chat_page.tsx` is deleted, the SSE route is unmounted, and the console has one
live-update mechanism.

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

- **Projection stage 2 — the frame table's two `kind` vocabularies**, and with it R2 and R3 of the
  numbering schedule ([chat_runtime_projection.md](chat_runtime_projection.md) § 2b). R1 landed; what
  is left is a cutover and a contract release, the second gated on the first converging, and nothing
  here depends on either. This is also where item
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
  provenance it wants exists now (`source_{first,last}_frame_seq`), so it starts cheaply — but it is
  a new subsystem, and the list above is what makes it stand on something finished.
- **Mid-turn steering**, which splits the turn's three jobs — mutex, recovery marker, accounting
  unit. Doing it before the neutral turn owns its own boundaries splits them in the wrong layer.
- **Sourcing the CLI from npm** ([the protocol-ownership plan](cli_protocol_ownership.md)).
  Unrelated to all of the above and independently dispatchable whenever someone wants it.

**Small enough to slot into any week with room:** slash commands and the room's session link, both
of which want C's route settled first because a posted Matrix event is permanent and federated.

## What is uncertain

- **Whether a coalesced refetch feels as good as the SSE stream.** C's ordering makes this moot if
  the increment lands first, and nobody has compared them on the real page.
- **Whether the neutral turn's usage shape survives a second backend.** #4169 designed an
  aggregatable shape from one example, which is the failure mode the neutral vocabulary exists to
  avoid, and nothing can check it until there is a second backend or a second stub.
- **Whether "a second backend works" is worth more code before anything can check it.** The stub
  harness is the cheapest test of that question and may still not be worth its cost.
