# Purge phase 1 — what was deleted, and what it cleared

Phase 1 of the legacy purge (<../../plans/next_month.md> § 1), run against production on 2026-08-16 between 23:52 and
23:57 UTC through the approval-gated `kubectl-passthrough-mcp` exec. This is the record of the run:
what the tables held, what made it safe to delete them, and which gates the deletion cleared.

## Before

Production was stamped **`0056`** with both console replicas on `devel-20260816225958-b5ad637` —
one release further along than the plan assumed, because #4198's image rolled and applied the drop
migration while the plan was being written. So the three-release sequence for
`session_messages.tool_uses` and `session_turns.usage` (#4193 unmapped, #4198 dropped) completed in
production, not only in CI.

| Table                 | Rows   |
| --------------------- | ------ |
| `sessions`            | 302    |
| `session_frames`      | 35,795 |
| `session_messages`    | 2,506  |
| `session_turns`       | 104    |
| `session_events`      | 2      |
| `session_prompts`     | 89     |
| `session_outbox`      | 2      |
| `matrix_held_batch`   | 0      |
| `matrix_conversation` | 1      |

Frames spanned 2026-08-12 to 2026-08-16. Of the 302 sessions, **301 were `failed`** and one was
`ready`.

## Why it was safe to delete without a quiesce step

The plan's step 2 asks for zero rows in `provisioning|ready|responding` before deleting, so that no
replica is writing into the tables as they go. One `ready` session held a lease on
`haku-console-5d7569996-d8ph9`, so the literal precondition did not hold — but each thing it exists
to protect was checked directly and held:

- **No turn in flight.** `ready`, not `responding`.
- **Nothing queued that could start one.** One unclaimed `session_prompts` row existed, and it
  belonged to a session that failed on 2026-08-13 — inert.
- **Nothing undelivered.** Both `session_outbox` rows had `sent_at` set, so the cascade lost no
  Matrix message.
- **No watermark decision.** `matrix_held_batch` was empty, so the plan's optional
  `matrix_sync_state.next_batch` advance did not arise.

The delete ran as one transaction: `DELETE 302`, and every dependent count above went to zero.
`matrix_conversation.session_id` is `SET NULL`, so the row survived pointing at nothing.

## Cycling the sandbox

Deleting the rows does not touch Kubernetes. `SandboxClaim` listed empty, but one `Sandbox` remained
— `haku-cp94x`, 7h36m old, a warm-pool member whose pod template was rendered at claim time.

**The `Sandbox` is what was deleted, not the pod.** A pod deleted under a live `Sandbox` regenerates
identically, because the template was already materialised; deleting the `Sandbox` is what re-renders
it. That distinction was learned the expensive way earlier the same day
(<2026_08_16_runtime_archaeology.md>).

## After

A replacement session provisioned within 90 seconds — `6bcf64ba-1bfa-4f63-a712-c65365dc8da7`,
`matrix`, `ready`, created 23:56:30, recording frames on a freshly rendered sandbox.

Its `projected_frame_seq` is **NULL**, which confirms the mechanism phase 2's tightening depends on:
`create()` never sets the attribute, so the ORM omits the column from the `INSERT`. A
`SET DEFAULT 0` therefore takes effect without the writer changing.

## What this cleared

Every gate whose condition was "no session that can still acquire a frame predates X". Those did not
self-clear — `_renew_lease` slides a claim's `shutdownTime` on every heartbeat, so a tended session
never ages out — and are now clear on the strongest evidence available: there are no old sessions at
all.

- The pre-cursor `adopt_open_turn` branch with `_recorded_completion` and `RESULT_FRAME_KIND`
- `reprojection.py`'s `NO_ROWS_AT_ALL` skip arm
- `_write_partial_frame` / `_clear_partial_frame`
- `transport.py`'s `HELLO_SECONDS` wait and its fallback — cleared by the sandbox cycle rather than
  by the delete
- `message_provenance.py` and its binary, whose whole subject was the 1,417 assistant rows that
  migration `0045` could not point

## What it did not clear

**The frame log is not permanently empty, and the difference matters.** Production is stamped `0056`
on a pre-cutover image, so `session_frames.frame_seq` is still `Identity(always=True)`: the
replacement session has been writing identity-numbered rows since 23:56:30, and every session created
before the numbering cutover will. The corpus refills.

What this run established is therefore not an empty table but a **demonstrated, authorised,
one-statement disposal**, which is what <../../plans/chat_runtime_projection.md> § 2 can lean on
instead. `sessions.frame_numbering` still goes, but not because the population is empty — the
population it exists for is the cutover's **own roll window**, where an old replica creates a session,
records identity frames, and a new replica adopts it. The purge does not remove that window; it makes
the consequence cheap to dispose of afterwards.

## What was not deleted

The Matrix room keeps every message already posted; the `haku_index` `git` corpus over haku-state is
untouched, so Haku's durable notes remain searchable. What went is the console's transcript and the
`chat` corpus indexed over it — conversational recall from before the cutover. `mcp_tool_calls` has
no foreign key into `sessions` and was not part of this.
