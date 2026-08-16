# Purging the legacy chat data, and the accommodations it forced

**Operator, 2026-08-16:** _"you messed up the first implementation, but I only used it for like 2
days and for nothing serious that I'd mind losing. I'm happy to drop old data including the derived
index if it helps us make the code clean and correct going forward. I expect it should be possible
to have constraints that actually express the requirements we will have met in production data once
we get all the old legacy out."_

So this plan takes the licence: **every `sessions` row goes**, the chat corpus with them, and the
schema is then tightened to say what the runtime actually guarantees. Nothing below is a migration
or a code deletion — it is the order in which those happen, and what each one is waiting for.

Delete this file once the last phase has landed.

## What is actually blocking today

**Phase 0 has landed, so nothing is blocking.** Both `haku-console` replicas run
`devel-20260816225958-b5ad637` (#4198, measured 2026-08-16) — the release carrying `0056` — and the
console applies its migrations at startup, so production is stamped `0056` and no serving replica
maps `tool_uses` or `session_turns.usage` any more. Step 1 of § The cutover is how to confirm that
against the database rather than trusting this line. There is no `0053`: `0054`'s `down_revision` is
`"0052"` (#4194 rechained it), so the chain runs `0052 → 0054 → 0055 → 0056`.

One correction to what the existing tombstones claim, and it is load-bearing:

- **`session_ttl_seconds` no longer bounds anything.** Three tombstones — <../console/x/session_store.py>
  line 479, <../console/x/reprojection.py> line 214, <../runtime/x/bridge/transport.py> line 36 —
  say their gate clears "within two hours" because a sandbox ages out at `session_ttl_seconds`
  (7200). It does not: `_renew_lease` slides the claim's `shutdownTime` on every heartbeat
  (<../console/debug/2026_08_16_runtime_archaeology.md> § The sandbox deadline that did not move), so
  a session a replica is tending never ages out. The live runner had been up since 2026-08-15T07:12
  when it was measured on 2026-08-16. **Those gates do not self-clear. Ending the session is what
  clears them**, which is phase 1 of this plan. Fix the wording wherever it appears
  (<chat_runtime_projection.md> § 407 and <../console/x/README.md> § 289 repeat it).

## What is lost, plainly

Deleting `sessions` deletes, by `ON DELETE CASCADE`: every `session_messages` row, every
`session_frames` row (35,760 of them), `session_turns` (99), the single `session_events` row,
`session_prompts`, `session_turn_prompts`, `session_outbox`, and — through `session_messages` —
`matrix_held_batch`. `matrix_conversation.session_id` is `SET NULL`, so the supervisor provisions a
replacement room session on its next pass.

The derived index goes with it. `haku_index`'s **`chat` corpus is Haku's recall over past
conversations**, and after this Haku can no longer search anything said before the cutover. Three
things soften that and should be said so the decision is made on the real cost:

- The **`git` corpus is untouched.** Haku's durable notes in haku-state remain indexed and
  searchable; what goes is conversational recall, not memory.
- The **Matrix room keeps its history.** Every message already posted is still in the room and still
  readable through Matrix; what goes is the console's transcript and the semantic index over it.
- The **approval ledger is untouched.** `mcp_tool_calls` has no foreign key into `sessions` and is
  not part of this purge.

One thing to check before running it, because it is outside this repo: **grep haku-state for
session UUIDs.** Nothing here writes them there, but a note Haku wrote by hand could quote one, and
that pointer would dangle.

## Inventory

Every accommodation that exists only because early data was written wrong, or because a roll has not
yet converged. "Roll" means it needs no data change at all — only the release that stops reading it.

| Accommodation                                                                                                                                                       | What it accommodates                                                                                    | How it dies                                                      | What replaces it                                                                              |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `session_messages.unpointable_reason` + `ck_session_messages_unpointable_{reason,exclusive}` + `MessageUnpointable`                                                 | Records that a row could not be pointed. Exists for the rows `0045` could not fill                      | **Rows**, then drop column                                       | `ck_session_messages_assistant_pointed` — nothing is ever unpointable again                   |
| `x/message_provenance.py`, `x/message_provenance_main.py`, their tests and BUILD targets                                                                            | The backfill that recovers those ranges by re-projecting                                                | **Rows** — with nothing to recover, delete both modules outright | none needed                                                                                   |
| `session_messages.source_{first,last}_frame_seq` nullable; `ck_session_messages_source_anchored` still `NOT VALID`                                                  | 1,417 assistant rows with no `agent_message_id`, plus prompt rows predating `set_message_source_frames` | **Rows**, then `VALIDATE CONSTRAINT` + a new role-split `CHECK`  | `role <> 'assistant' OR source_first_frame_seq IS NOT NULL`, validated                        |
| `session_messages.tool_uses`                                                                                                                                        | Claude's wire spelling, superseded by `tool_calls` in `0047`                                            | **Roll** — unmap in one release, `drop_column` in the next       | none                                                                                          |
| `session_messages.tool_calls` and `message_view`'s `recorded or message.tool_calls`                                                                                 | A message written before `session_events` had rows                                                      | **Rows** make the fallback unreachable; then unmap, then drop    | `session_events` is the only record of a call, with its answer                                |
| `session_turns.usage`                                                                                                                                               | Claude's `usage` sub-object, superseded by the four columns in `0049`                                   | **Roll** — phase 2 landed as #4193, this is phase 3              | none                                                                                          |
| `sessions.projected_frame_seq` nullable                                                                                                                             | Sessions predating `0051`'s cursor; set on exactly one row today                                        | **Rows** + expand/contract to `NOT NULL DEFAULT 0`               | `NOT NULL` — "nothing projected yet" becomes `0`, not `NULL`                                  |
| The pre-cursor adoption branch: `adopt_open_turn`'s second arm, `_recorded_completion`, `_ended_at_frame`, `RESULT_FRAME_KIND`'s import in `session_store.py`       | An open turn whose session has a `NULL` or pre-turn cursor                                              | **Rows** (no live session can be in that state afterwards)       | `adopt_open_turn` has one path: re-project from the cursor                                    |
| `session_frames.partial`, `uq_session_frames_partial`, `_write_partial_frame`, `_clear_partial_frame`, two `partial.is_(False)` filters, `SessionFrameView.partial` | The console's reconstruction of a streaming answer, superseded by recorded deltas                       | **Rows** + **roll** — unmap, then drop                           | The frame log holds only what crossed the wire; `SessionFrame`'s "third thing" goes           |
| `session_frames.runner_seq` carried by 0 of 35,760 frames                                                                                                           | Not legacy data — a **runner image** predating #4166, pinned by the `Sandbox` at claim time             | **Cycling the runner**, which is what deleting the session does  | `direction = 'to_agent' OR kind = 'setup_output' OR runner_seq IS NOT NULL`                   |
| `sessions.surface` nullable; `ck_sessions_room_is_matrix` + `ck_sessions_matrix_has_room` split "because a legacy row has neither"                                  | Rows written before `0030`                                                                              | **Rows**                                                         | `surface NOT NULL` and one equivalence: `(surface = 'matrix') = (room_id IS NOT NULL)`        |
| `EventProvenance.AUTHORED`, `conversation_events.Authored`, `conversation_records.ConsoleAuthored`, the `authored` arm in `session_events.row` and `reprojection`   | An arm with **no writer** — constructed only in tests                                                   | A **decision** (see below), then rows make it free               | If deleted: `provenance` is a constant column and drops; both frame columns become `NOT NULL` |
| `reprojection`'s `SKIPPED` arm for a turn with no events                                                                                                            | Turns served before the release that writes `session_events`                                            | **Rows**                                                         | A turn with frames and no rows is drift, and is reported as drift                             |
| `transport.py`'s `HELLO_SECONDS` wait and its fallback                                                                                                              | Runner images predating the `Hello` envelope                                                            | **Cycling the runner**                                           | The handshake is required                                                                     |
| 80 of 99 `session_turns` whose `last_frame_seq` points at a trailing `command_lifecycle` frame                                                                      | #4189 fixed this forward and did not backfill                                                           | **Rows**                                                         | none — but `reprojection`'s per-turn alignment becomes meaningful                             |
| `SessionStatus.IDLE` with no writer                                                                                                                                 | Nothing — a **forward** tombstone waiting on the `0054` roll, not legacy data                           | **Roll** (phase 0), then write the first `idle` row              | `LIVE_SESSION_STATUSES` splits into "worth keeping" and "has a lease"                         |

### Trivially removable now, no data dependency

- **`adopt_open_turn`'s pre-cursor branch** was gated on `0051`'s release converging. Production is
  at `0056`, so `0051` converged long ago and the gate is clear on its own terms — the branch is
  reachable only by a session with an open turn and a stale cursor, which the ordinary case cannot
  produce. It is still safest to take it in phase 2, but it is not blocked on the purge.
- **`transport.py`'s `HELLO_SECONDS` wait** is blocked only on the runner image, not on any row.
- **`include_in_schema=False` and `_legacy_pending`** are already gone from the tree.

## The target schema

What the DDL looks like when this is done. Grouped by table; the phase each statement belongs to is
in § Sequence.

```sql
-- sessions: a session's surface is known, and its cursor is a number rather than a maybe.
ALTER TABLE sessions ALTER COLUMN surface SET NOT NULL;
ALTER TABLE sessions DROP CONSTRAINT ck_sessions_surface;
ALTER TABLE sessions ADD  CONSTRAINT ck_sessions_surface CHECK (surface IN ('spa','matrix'));
ALTER TABLE sessions DROP CONSTRAINT ck_sessions_room_is_matrix;
ALTER TABLE sessions DROP CONSTRAINT ck_sessions_matrix_has_room;
ALTER TABLE sessions ADD  CONSTRAINT ck_sessions_matrix_room
  CHECK ((surface = 'matrix') = (room_id IS NOT NULL));
ALTER TABLE sessions ALTER COLUMN projected_frame_seq SET DEFAULT 0;
ALTER TABLE sessions ALTER COLUMN projected_frame_seq SET NOT NULL;
```

```sql
-- session_messages: an assistant row is always pointed. A user row is unpointed exactly while its
-- prompt is unclaimed, which is a live state and stays nullable.
ALTER TABLE session_messages VALIDATE CONSTRAINT ck_session_messages_source_anchored;
ALTER TABLE session_messages ADD CONSTRAINT ck_session_messages_assistant_pointed
  CHECK (role <> 'assistant' OR source_first_frame_seq IS NOT NULL);
ALTER TABLE session_messages DROP CONSTRAINT ck_session_messages_unpointable_exclusive;
ALTER TABLE session_messages DROP CONSTRAINT ck_session_messages_unpointable_reason;
ALTER TABLE session_messages DROP COLUMN unpointable_reason;
ALTER TABLE session_messages DROP COLUMN tool_uses;
ALTER TABLE session_messages DROP COLUMN tool_calls;
```

```sql
-- session_events: with no `authored` arm, the discriminator is a constant and the frames are facts.
ALTER TABLE session_events ALTER COLUMN source_first_frame_seq SET NOT NULL;
ALTER TABLE session_events ALTER COLUMN source_last_frame_seq  SET NOT NULL;
ALTER TABLE session_events DROP CONSTRAINT ck_session_events_provenance_frames;
ALTER TABLE session_events ADD  CONSTRAINT ck_session_events_frames
  CHECK (source_first_frame_seq <= source_last_frame_seq);
ALTER TABLE session_events DROP CONSTRAINT ck_session_events_provenance;
ALTER TABLE session_events DROP COLUMN provenance;
```

```sql
-- session_frames: every row is the wire, and every wire row from the agent carries the runner's
-- number. `setup_output` is the console's own row and is exempt by name.
DROP INDEX uq_session_frames_partial;
ALTER TABLE session_frames DROP COLUMN partial;
ALTER TABLE session_frames ADD CONSTRAINT ck_session_frames_runner_seq_direction
  CHECK (runner_seq IS NULL OR direction = 'from_agent');
ALTER TABLE session_frames ADD CONSTRAINT ck_session_frames_wire_numbered
  CHECK (direction = 'to_agent' OR kind = 'setup_output' OR runner_seq IS NOT NULL);
```

```sql
-- session_turns
ALTER TABLE session_turns DROP COLUMN usage;
```

**Totals.** 4 columns become `NOT NULL` (`sessions.surface`, `sessions.projected_frame_seq`,
`session_events.source_{first,last}_frame_seq`). 6 columns drop. 4 constraints are added and 1 is
promoted from `NOT VALID`. 8 code branches and 2 whole modules go.

**Two honest limits.** `session_frames.runner_seq` keeps its partial index — a `to_agent` frame
legitimately has no runner number, so `WHERE runner_seq IS NOT NULL` stays right. And
`session_messages.source_first_frame_seq` cannot become `NOT NULL` outright: `enqueue_prompt` writes
the prompt's row before the frame it goes out as exists, and a prompt whose session ends before any
turn claims it (`PromptFate.LOST`) keeps a `NULL` range forever. That is a defined absent state, not
legacy — which is exactly why the replacement is split by `role`.

## Sequence

Five phases. The ordering constraints that force this shape, stated once:

1. **A column cannot be tightened to `NOT NULL` before the violating rows are gone.** Phase 1 does
   the deleting; every tightening is phase 2 or later.
2. **The console rolls `maxUnavailable: 0` with migrations at startup, so a replica on the previous
   image runs against the new schema for the length of the roll.** The ORM maps every column, and
   SQLAlchemy selects every mapped column — so "an old replica still `SELECT`s it" is true of _any_
   column until a release removes the mapping. **Every `DROP COLUMN` is therefore two releases**:
   one that unmaps and deletes the readers, one that drops. This is exactly what #4192/#4193 were
   about.
3. **Adding a `CHECK` needs no split** when the tables are empty and the previous image's writers
   already satisfy it. After phase 1 they are and it does — verified per constraint below. So the
   tightening rides along with the code-only release rather than waiting for a third one.
4. **`SET NOT NULL` on `projected_frame_seq` does need the split**, because the previous image can
   still insert a session with no cursor. `SET DEFAULT 0` first, `SET NOT NULL` a release later.
5. **The runner is cycled by deleting the session, not by deleting its pod.** The pod template is
   rendered from the `SandboxTemplate` when the claim is made and the tag is pinned there, so a
   deleted pod comes back on the same image (measured 2026-08-16, <../TODO.md>). The `runner_seq`
   constraint therefore waits on phase 1, not on a separate operation.

### Phase 0 — roll the console to current `devel` — landed

No data change and no new migration: it applied `0054`, `0055` and `0056`, and put every replica on
an image that no longer reads `tool_uses` or `session_turns.usage`. That was the precondition for
everything below, since every drop depends on the currently-serving image being one that does not
select the column. Confirm it against the database with step 1 of § The cutover before running
phase 1.

### Phase 1 — quiesce, delete, cycle

The destructive step, and the only one. Section § The cutover has the commands.

### Phase 2 — the code-only release, plus the additive tightening

One release, no `DROP COLUMN`:

- Unmap `session_messages.{unpointable_reason,tool_uses,tool_calls}`, `session_turns.usage`,
  `session_frames.partial` from `database_schema.py` and delete every reader and writer.
- Delete `x/message_provenance.py`, `x/message_provenance_main.py`, their tests, their BUILD targets,
  and `MessageUnpointable`.
- Delete `adopt_open_turn`'s pre-cursor branch, `_recorded_completion`, `_ended_at_frame`, and the
  `RESULT_FRAME_KIND` import in `session_store.py` (the constant survives in
  `x/claude_code/projection.py`).
- Delete `_write_partial_frame`, `_clear_partial_frame`, their call sites, both
  `partial.is_(False)` filters, and `SessionFrameView.partial`.
- Delete `reprojection`'s "no rows at all" `SKIPPED` arm — a turn with frames and no events is drift
  from here on.
- Delete `message_view`'s `recorded or message.tool_calls` fallback.
- Delete `transport.py`'s `HELLO_SECONDS` wait and its fallback.
- Migration `0057`, additive only: `sessions.surface` `SET NOT NULL`; the surface/room equivalence;
  `VALIDATE CONSTRAINT ck_session_messages_source_anchored`;
  `ck_session_messages_assistant_pointed`; both `session_frames` runner-seq checks;
  `projected_frame_seq SET DEFAULT 0` and the `UPDATE … SET 0 WHERE NULL`.

Each of those additions is safe against the phase-0 image, and the reason is specific rather than
general:

| Addition                                       | Why the phase-0 replica cannot violate it                                                                                                                    |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `surface NOT NULL`                             | `SessionStore.create` always sets `surface` from the variant's `surface_column`                                                                              |
| `(surface = 'matrix') = (room_id IS NOT NULL)` | Same call site sets `room_id` from the same variant                                                                                                          |
| `ck_session_messages_assistant_pointed`        | Both `_open_assistant` call sites pass a `frame_seq`; there is no third writer of an assistant row                                                           |
| `VALIDATE … source_anchored`                   | The table is empty, and `update_assistant` only ever widens a range whose near end `begin_assistant` set                                                     |
| `ck_session_frames_wire_numbered`              | Holds only because phase 1 cycled the runner — **verify the gate query first**                                                                               |
| `projected_frame_seq SET DEFAULT 0`            | `create()` never sets the attribute, so the ORM omits it from the `INSERT` and the default applies. Confirm with the verification query rather than assuming |

### Phase 3 — the drop release

Migration `0058`, once phase 2 has converged: the six `DROP COLUMN`s, `DROP INDEX
uq_session_frames_partial`, the two `unpointable_*` constraints, and
`projected_frame_seq SET NOT NULL`.

### Phase 4 — the `authored` decision, and `session_events`

`EventProvenance.AUTHORED` is the one item that **cannot be specified until it is decided** —
<next_month.md> § 3 has the two design documents that disagree. What this plan adds is that
**the purge makes the decision free in both directions**: production holds one `session_events` row
and it is `frame_range`, so after phase 1 there is nothing to migrate whichever way it goes.

If the arm is deleted: `provenance` becomes a constant column and drops, both frame columns become
`NOT NULL`, and `ck_session_events_provenance_frames` collapses from three clauses to one. Those
statements go in a phase-2-style code release followed by a phase-3-style drop, on their own
schedule. If the arm gets a writer instead, nothing here changes.

### Phase 5 — `SessionStatus.IDLE`'s writer

Independent of the purge and gated only on phase 0's roll, which has landed. <next_month.md> § 1
owns it.

### Phase 6 — squash the chain, and the tests that exist only to guard it

The purge's own reward, asked for by the operator: _"once we've migrated prod to proper schema shape
and constraints without weird legacy or wrong data I'd want to drop the load of keeping around all
the migration tests."_ It is last because its gate is everything above — a chain can only be
squashed once the only database that will ever replay it is stamped past the end of it.

**Gate:** production stamped at phase 3's head, every replica on an image at or after the release
that carried it. That is step 1 of the cutover asked one revision later.

**The load, measured on `devel` at `b5ad637b43`:** 46 revisions from `0010` to `0056`, with `0053`
missing because #4194 renamed it out of a fork; six dedicated migration tests; and three further
test modules that drive alembic for other reasons.

**A squash here is not a new technique** — `0010` is one, and its docstring records how: the
revision id of the deployed head is retained, so a database already stamped at it is a no-op while a
fresh database creates the frozen schema directly. Repeating that at phase 3's head collapses
`0011`–head into one file and leaves production untouched.

The six tests split three ways, and the split is the point — "drop the migration tests" deletes two
things that are not about migration at all:

| Test                                         | Rev    | After the squash                                                                                                                                                                                                                    |
| -------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_message_tool_calls_migration.py`       | `0047` | **Goes.** Already moot — `0056` dropped the column it backfills from, which its own docstring says                                                                                                                                  |
| `test_neutral_turn_usage_migration.py`       | `0049` | **Goes.** Same: `0056` dropped `session_turns.usage`                                                                                                                                                                                |
| `test_session_claim_cleaned_at_migration.py` | `0048` | **Goes.** A backfill of rows phase 1 deletes                                                                                                                                                                                        |
| `test_frame_runner_seq_migration.py`         | `0050` | **Goes.** Asserts a nullable column an old writer could omit; phase 2 makes it not-nullable                                                                                                                                         |
| `test_session_idle_status_migration.py`      | `0054` | **Becomes a constraint test.** Both assertions are about what `ck_sessions_status` admits, which the baseline will state directly. Not before phase 5 ships, since until then the widening is the live half of a two-release change |
| `test_state_index_migration.py`              | `0037` | **Stays, rebased.** It is not a migration test: it compares <../state_index/schema.py> against what the deployed database gets, and nothing else does. Point it at the new baseline                                                 |

That is 473 of the six files' 624 lines deleted outright, and no coverage lost that a database at
head can still exercise — because the two tests that assert the property a squash actually endangers
already exist, in `test_agent_authority_schema.py`:
`test_fresh_baseline_matches_sqlalchemy_metadata` (`compare_metadata` against the ORM returns empty)
and `test_database_already_at_head_is_unchanged` (re-applying at head is a no-op). Those are what
<../../STYLE.md> § Testing asks for in place of per-migration change-detectors, and they are the two
that must pass **before** the squash lands as well as after — a squash whose baseline disagrees with
the ORM is a console that cannot boot.

**What the squash does not buy.** Every migration written after it is a migration again, with the
same expand/contract cost, so this is a one-time collection of a debt rather than a change of
policy. And it forecloses nothing except replaying history on a database older than production —
which, after phase 1, is no database that exists.

## The cutover

Run through an approval-gated console exec against the production database. Each step says what it
returns when it worked.

### 1. Confirm phase 0 has landed

```sql
SELECT version_num FROM alembic_version;
```

Must return `0056`. Anything earlier means phase 0's roll has not converged after all — stop,
because nothing below is safe yet.

```bash
kubectl get pods -n haku-console -o jsonpath='{.items[*].spec.containers[0].image}'
```

Every tag's commit suffix must be at or after the release carrying `0056`.

### 2. Quiesce

Close every live session through the console UI (or `request_close`), so the turn loop ends its turn
and the outbox drains rather than being deleted mid-write.

```sql
SELECT session_id, surface, status, lease_holder, lease_expires_at
  FROM sessions WHERE status IN ('provisioning','ready','responding');
```

Must return **zero rows** before continuing. A row here means a replica is still writing into the
tables about to be deleted.

Optionally decide the Matrix watermark first. Deleting the sessions cascades away
`matrix_held_batch`, and the watermark it was holding never moved — so the messages in that batch
are re-offered to the replacement session. If that is unwanted, advance it by hand before the
delete:

```sql
SELECT user_id, next_batch FROM matrix_held_batch;
-- to accept the held batch as handled instead of re-delivering it:
UPDATE matrix_sync_state s SET next_batch = h.next_batch
  FROM matrix_held_batch h WHERE h.user_id = s.user_id;
```

### 3. Record what is about to go

```sql
SELECT
  (SELECT count(*) FROM sessions)                                   AS sessions,
  (SELECT count(*) FROM session_messages)                           AS messages,
  (SELECT count(*) FROM session_frames)                             AS frames,
  (SELECT count(*) FROM session_turns)                              AS turns,
  (SELECT count(*) FROM session_events)                             AS events,
  (SELECT count(*) FROM session_outbox WHERE sent_at IS NULL)       AS unsent_replies,
  (SELECT count(*) FROM state_index.chat_sessions)                  AS indexed_sessions,
  (SELECT count(*) FROM state_index.chat_chunks)                    AS indexed_windows;
```

Expected shape from the 2026-08-16 measurements: `frames` ≈ 35,760, `turns` = 99, `events` = 1.
`unsent_replies` should be 0 after step 2; a non-zero count is a reply the room will never hear, and
is the one thing worth reading before deleting.

### 4. Delete

One transaction, so search never returns a pointer into a session that is gone.

```sql
BEGIN;
DELETE FROM state_index.chat_chunks;
DELETE FROM state_index.chat_sessions;
DELETE FROM state_index.chunks WHERE corpus = 'chat';
DELETE FROM sessions;
COMMIT;
```

`chat_chunk_messages` goes by its own cascade from `chat_chunks`. The third statement is the vector
cache — the sync's own `forget_chat_sessions` sweep does not touch it, so without this the
embeddings survive as unreachable cache. Delete it anyway: the corpus is gone and a cached vector
for text nobody will say again is dead weight.

**Do not delete the `git` corpus.** `state_index.chunks WHERE corpus = 'git'`, `git_tip` and
`git_sync_state` are haku-state's index and are not part of this.

Verify:

```sql
SELECT
  (SELECT count(*) FROM sessions)                  AS sessions,
  (SELECT count(*) FROM session_messages)          AS messages,
  (SELECT count(*) FROM session_frames)            AS frames,
  (SELECT count(*) FROM session_events)            AS events,
  (SELECT count(*) FROM state_index.chat_chunks)   AS windows,
  (SELECT count(*) FROM state_index.chunks WHERE corpus = 'chat') AS chat_vectors,
  (SELECT count(*) FROM state_index.chunks WHERE corpus = 'git')  AS git_vectors;
```

Every count zero except `git_vectors`, which must be unchanged.

### 5. Let the runner cycle, and prove it

The supervisor provisions a replacement session on its next pass, which makes a new `SandboxClaim`
and therefore renders a new pod from the **current** `SandboxTemplate`.

```bash
kubectl get pods -n haku-sandbox -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'
```

The runner's tag must now be at or after the release carrying #4166. Then send a Matrix message and
check that the frames it produces are numbered:

```sql
SELECT count(*) FILTER (WHERE runner_seq IS NOT NULL) AS numbered,
       count(*) FILTER (WHERE direction = 'from_agent' AND kind <> 'setup_output'
                          AND runner_seq IS NULL)     AS unnumbered
  FROM session_frames;
```

**`unnumbered` must be 0.** This is the gate for `ck_session_frames_wire_numbered`; if it is
non-zero the runner is still stale and that constraint must be left out of phase 2.

### 6. Prove the phase-2 constraints before writing them

Run each as a query first. Every one must return 0.

```sql
SELECT count(*) FROM sessions WHERE surface IS NULL;
SELECT count(*) FROM sessions WHERE (surface = 'matrix') <> (room_id IS NOT NULL);
SELECT count(*) FROM session_messages
  WHERE role = 'assistant' AND source_first_frame_seq IS NULL;
SELECT count(*) FROM session_messages
  WHERE source_last_frame_seq IS NOT NULL AND source_first_frame_seq IS NULL;
SELECT count(*) FROM session_events WHERE provenance <> 'frame_range';
SELECT count(*) FROM session_frames WHERE runner_seq IS NOT NULL AND direction <> 'from_agent';
```

And the one that decides whether `projected_frame_seq SET DEFAULT 0` is enough on its own — after
the default is added, create a session through the console and read it back:

```sql
SELECT session_id, projected_frame_seq FROM sessions ORDER BY created_at DESC LIMIT 1;
```

`0`, not `NULL`, means the ORM omitted the column and the default applied, and `SET NOT NULL` can go
in phase 3. `NULL` means the ORM sends an explicit `NULL` and the writer has to change first — in
which case `create()` sets `projected_frame_seq=0` in phase 2 and the `SET NOT NULL` waits for
phase 3 as planned regardless.

### 7. After phase 3

```sql
SELECT column_name FROM information_schema.columns
 WHERE table_name IN ('sessions','session_messages','session_turns','session_frames','session_events')
   AND column_name IN ('unpointable_reason','tool_uses','tool_calls','usage','partial','provenance');
```

Zero rows. And:

```sql
SELECT conname, convalidated FROM pg_constraint
 WHERE conrelid = 'session_messages'::regclass AND contype = 'c';
```

`ck_session_messages_source_anchored` must be `convalidated = true`.

## Deliberately out of scope

- **`session_messages.agent_message_id` stays nullable.** It is not a legacy accommodation: a
  synthesised assistant row (text that arrived only on the `result` frame) legitimately has none,
  and a second backend need not supply one at all. What the purge removes is the _population_ — the
  1,417 rows without it — and the historical comments that count them. Retiring the column belongs
  to <chat_runtime_cleanup.md> § The backend seam.
- **`capabilities.py`** is legacy in a different sense (the haku-ui launch migration) and has its own
  tombstone and its own gate.
- **Promoting anything on `session_frames.kind`.** The two-vocabulary defect in that column is real
  and is <chat_runtime_projection.md> § 2b's; dropping `partial` removes the **third** meaning and
  nothing more.
