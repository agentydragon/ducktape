# Chat runtime cleanup

What is left of a design review of `haku/console/x/` and the schema it writes, taken after the
runtime had been built iteratively across a dozen PRs. Findings are deleted from this file as they
land, so everything below is work that has not been done. Nothing here is a bug report: the runtime
works and is in production.

Ordered by payoff, not by size.

## The console drives the CLI protocol itself

Decided 2026-08-12 and written up separately, because it is a direction rather than a cleanup:
<cli_protocol_ownership.md>. Most of what this review found sat on that seam — frames the SDK's
typed layer dropped, and who parses them — so read it first; what remains of that direction is
tracked there rather than here.

## Mid-turn steering works and we are not using it

Measured, not inferred (<../cli_protocol/probes/steering.py>, 2026-08-12): a prompt written
to the CLI while a turn is running is **absorbed at the next tool boundary**, the model acts
on it, and one `result` frame covers both prompts. <matrix_chat_runtime.md> R2.2a defers this
as having "no native mechanism"; that is now corrected there.

Nothing on our side is preventing it either — writing a prompt to the CLI is a bare
`transport.write()` with no interlock. What prevents it is the shape of our loop: `_run_turn` drains
to the `result` frame before looking for the next prompt.

So `MatrixTurns.offer` can stop refusing batches during a turn (R2.2 becomes fold-into-turn)
and "actually, skip the calendar part" reaches Haku while it is working. The turn model landed the
shape this needs — `claude_chat_turn_prompts` is many-to-one already — and deliberately did not turn
it on: admission still refuses a second prompt while a turn is open, and a test says so.

A fold is confirmable rather than merely visible in what the model does next: `ClaudeCli.query`
stamps a `uuid` on the prompt, which is what makes the CLI report `command_lifecycle`, and
`completed` before the turn's `result` means folded.

Two cautions. A turn with no tool call has no boundary to absorb at, so the fallback to
next-turn delivery stays. And the events the bundled CLI documents are `@internal`, so this
wants the same version-pinning discipline as the FastMCP adapter.

**The abort path needs `cancel_queued`.** A bare `interrupt` cancels the running turn and the
CLI then **starts the next queued prompt** — measured, <../cli_protocol/probes/steering.py>. Our
abort means "stop, and drop what I asked for next", which is `interrupt` with
`cancel_queued: true`; it reaches only uuid-stamped commands, which ours now are.

## The prompt queue's compatibility half is still in place

`claude_chat_prompts` is the queue — one row per prompt, `claimed_at` for whether it is still
waiting, a partial unique index making "one in flight per session" a property of the schema. What
still runs beside it is the shape it replaced: the transcript row is minted `pending`, and
`ClaudeChatStore` still falls back to scanning the transcript for one, so a prompt an old replica
accepted mid-roll is still answered. Both are tombstoned in the code.

Once the roll converges, write the transcript row final and drop the `_legacy_pending` scan.
`'pending'` stays in `ck_claude_chat_messages_status` — dropping it is a destructive migration for
no benefit.

## `tool_uses` is a column with almost no reader left

`claude_chat_messages.tool_uses` holds id/name/input and no result. The frames beside it hold both,
verbatim, so `ClaudeChatSessionView` takes each call **and** its result from the rollout, joined by
the agent's own `msg_…` id, which the transcript row records. The column is read only for a row with
nothing to point at: one written before that pointer existed, or one the console synthesized rather
than observed (a turn whose text arrived only on the `result` frame).

**Deleting it takes two more releases.** `tool_uses` is `nullable=False` with only a Python-side
`default=list`, so the ORM attribute cannot go until the column has a server default
(`SET DEFAULT '[]'::jsonb`), and the `drop_column` cannot share a release with that — an old
replica's `_message_view` selects the mapped column by name. The synthesized-message case has to
stop needing it first: either those rows get their calls recorded as frames, or they keep having
none, which is what they have today.

## An expired lease should mean unowned, not dead

`lease_expires_at` is a creator-granted provisioning budget before a runner attaches
(`PROVISION_LEASE`, ten minutes) and an owner heartbeat afterwards (`LEASE_TTL`, ninety seconds), and
`lease_holder` now says which of the two is running and which pod holds it. What it still means when
it expires is **dead**: `expire_stale_leases` fails the session and the supervisor provisions a
replacement. <cli_protocol_ownership.md> wants it to mean **adoptable** instead — which cannot land
before an adopter exists, since reinterpreting expiry on its own leaves a room silent behind a
healthy-looking row.

## `ClaudeChatStore` is a god object

Twenty-odd methods across session lifecycle, prompt queue, transcript, frames, turns, leases and
claim-cleanup bookkeeping. It splits along the seams the turn table and the prompt queue created:
sessions/leases, prompts, transcript, rollout. Not as a PR of its own — a standalone reshuffle has no
acceptance criterion and would conflict with everything else here; each split lands with the change
that creates its seam.
