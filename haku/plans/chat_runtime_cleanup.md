# Chat runtime cleanup

**Status: proposed.** A design review of `haku/console/x/` after it was built iteratively
across a dozen PRs, plus the schema it writes. Each item says what is wrong, why it is wrong
rather than merely unusual, and what it costs to fix. Nothing here is a bug report: the
runtime works and is in production. This is the cruft that accumulated under it.

Ordered by payoff, not by size.

## 0. The console drives the CLI protocol itself

Decided 2026-08-12 and written up separately, because it is a direction rather than a cleanup:
<cli_protocol_ownership.md>. Four of the items below turn out to be the same seam — §2 needs a
frame field the SDK never sends, §2a and §4 are about frames its typed layer drops, and §5 is
about who parses them — so read that first and treat the rest as what remains once it lands.

## 1. There is no turn, and much of the awkwardness is that absence

`_run_turn`'s stack frame is already the turn. Because it is only a stack frame, the things
that want to name a turn have to name something else instead:

- `ChatSessionStatus` mixes session lifecycle (`provisioning`/`ready`/`closing`/`closed`/
  `failed`) with turn state (`responding`), so `update_assistant` re-asserts
  `chat.status = RESPONDING` **on every delta** — a session-row write per delta to hold a
  derived flag true.
- `enqueue_prompt` sets `RESPONDING` before any turn starts, so `request_abort` will accept an
  abort for a turn that does not exist and `MatrixTurns.offer` refuses batches on it.
- The comment in `handle_runner` — "an abort notified just as the previous turn ended … needs
  the abort to name a turn rather than a session" — is a bug documented instead of fixed.
- `claude_chat_frames` has nothing to slice on, so Phase 5's `read_turn` has no key.

**What a turn is**: one exchange, from the harness handing the agent a prompt to the harness
having a final answer or a failure — exactly `_run_turn`'s span. It contains many assistant
messages, many tool uses, many model round trips. It is not a model round trip (the SDK's
`ResultMessage.num_turns` counts _those_, which live inside ours), not a message, and not a
Matrix message (R2.1 coalesces a batch into one prompt).

**Store it as a bracket, not a label.** A `turn_id` stamped on each frame would write our
interpretation into the record of the wire, and the wire does not agree with it: the CLI can
fold a second prompt into a running turn (§2), so one `result` frame can cover two prompts.
The turn row records a **range** instead — `first_frame_seq`, `last_frame_seq | None` — so
`read_turn` is a range query, the log stays verbatim, and re-bracketing later is an update to
our table rather than a rewrite of the record.

```text
turn(turn_id, session_id, first_frame_seq, last_frame_seq | None,
     started_at, ended_at | None, outcome, cost/usage/duration)
turn_prompt(turn_id, message_id)   -- many prompts per turn once folding is used
```

`ended_at IS NULL` on a session with no live holder is exactly "abandoned mid-flight", which
is today representable only as a lie: a live status nobody is maintaining.

Fixes, in order: the abort race; `responding` becomes derived; the partial frame's uniqueness
becomes per-turn rather than a per-session index enforcing a per-turn fact; `ResultMessage`'s
cost/usage/duration gets somewhere to live instead of being read for the error check and
discarded; and re-adoption gets a durable handle for the in-flight exchange
(<cli_protocol_ownership.md> wants to route an adopted turn "by session", which is the wrong key
when a session can outlive many turns).

## 2. Mid-turn steering works and we are not using it

Measured, not inferred (<../debug/mid_turn_steering_probe.py>, 2026-08-12): a prompt written
to the CLI while a turn is running is **absorbed at the next tool boundary**, the model acts
on it, and one `result` frame covers both prompts. <matrix_chat_runtime.md> R2.2a defers this
as having "no native mechanism"; that is now corrected there.

Nothing on our side was preventing it either — `ClaudeSDKClient.query()` is a bare
`transport.write()` with no interlock. What prevents it is the shape of our loop:
`receive_response()` drains to `ResultMessage` before looking for the next prompt.

So `MatrixTurns.offer` can stop refusing batches during a turn (R2.2 becomes fold-into-turn)
and "actually, skip the calendar part" reaches Haku while it is working.

Two cautions. A turn with no tool call has no boundary to absorb at, so the fallback to
next-turn delivery stays. And the events the bundled CLI documents are `@internal`, so this
wants the same version-pinning discipline as the FastMCP adapter.

A third — that folding was observable only by its effect — no longer holds.

**That last one is now solved.** `command_lifecycle` is emitted for any inbound user frame
carrying a `uuid`, which the SDK never sent — not, as first read, gated behind the
`msg_lifecycle_v1` capability `system/init` advertises (`initialize` has no field for declaring
client capabilities at all). `ClaudeCli.query` mints one and returns it, so a fold is
confirmable: `completed` before the turn's `result` means folded, after means it started a
fresh turn.

Still open from that list: `interrupt_cancel_queued_v1`. Interrupt and queued messages
interact, and our abort path knows nothing about a prompt sitting in the CLI's queue.

## 2a. `system/task_*` frames are a status line we already store and ignore

The same run showed `system/task_started` and `system/task_notification` carrying
`tool_use_id`, a `task_type`, and a human-readable `description` — "Sleep 4 seconds (step 1)".
That is R6's "what is Haku doing right now" without inventing anything: the frames are already
in `claude_chat_frames`, and the SDK's `Message` union has no variant for them, so the typed
layer drops them and only the raw store has them. Today the room's only progress signal is the
sandbox bootstrap's stdout, which stops the moment the session starts working.

Folding is also what makes §1's `turn_prompt` many-to-one rather than a column.

## 3. The user message row is a queue _and_ a transcript

`next_prompt` marks a user row `COMPLETE` when it **hands the prompt to the model**; for an
assistant row `COMPLETE` means the answer is finished. One enum, opposite meanings,
disambiguated only by `role` — and the states already partition by role, since `PENDING` is
only ever a user row and `STREAMING`/`FAILED` only ever assistant.

That is downstream of the row doing two jobs: "one prompt in flight" is enforced by scanning
messages for `PENDING`, and dequeue is `FOR UPDATE SKIP LOCKED` over the transcript. Splitting
the pending prompt from the message log lets the transcript be append-only and deletes a class
of status reasoning. Touches the SPA, so it is the deepest of these.

## 4. `tool_uses` is now a lossy copy of the rollout

`claude_chat_messages.tool_uses` holds id/name/input and no result. Since the frame store
landed, the frames hold both, verbatim. One reader —
`frontend/x/claude_chat_page.tsx` — so re-sourcing it from frames would also give the SPA the
tool _results_ it cannot show today, after which the column goes.

## 5. Frame recording should move onto the transport

`RecordingWebSocket` decorates the socket to avoid "the shared transport learning about the
console's database" — but `WebSocketTransport` already takes `on_progress: ProgressSink`,
which is exactly that shape. An `on_frame` callback beside it is one parse instead of two, and
one place that knows the envelope instead of two. The rule the decorator was written to
respect is one the file does not itself follow.

## 6. The lease means two things and never says who holds it

`lease_expires_at` is a creator-granted provisioning budget before a runner attaches
(`PROVISION_LEASE`, ten minutes) and an owner heartbeat afterwards (`LEASE_TTL`, ninety
seconds). It records _when_ but never _who_: `_REPLICA` is already computed in
`matrix_session.py` for room announcements, so writing it would make the failure say which pod
died — and adoption arbitration will want it anyway. <cli_protocol_ownership.md> separately wants
an expired lease to mean **unowned** (adoptable) rather than **dead**.

## 7. `ClaudeChatStore` is a god object

Twenty-odd methods across session lifecycle, prompt queue, transcript, frames, leases and
claim-cleanup bookkeeping. It splits along the seams §1 and §3 create: sessions/leases,
prompts, transcript, rollout.

## 8. Smaller, mechanical

- `abort_session` reaches into `service._store`.
- `_sse_stream` detects change by comparing `model_dump_json()` strings — serializing the whole
  view twice per wake, and any timestamp churn defeats the comparison.
- `SpaSession`/`MatrixSession` variants plus the `ChatSurface` column enum plus an `isinstance`
  mapping is close to the aliasing STYLE warns about; a `column_value` on the variants would
  do.
- `matrix_conversation.session_id` and `claude_chat_sessions.room_id` are two places the same
  binding lives. The intended split is "conversation = current pointer, session.room_id =
  history"; nothing enforces or states it, and it will drift.

## Done since the review

- **The Matrix callbacks were wired backwards** and are not any more. `ClaudeChatService` took
  three optional callbacks that existed only for Matrix, fired them for every session, and
  each implementation opened by loading the current room binding and comparing its
  `session_id` — the session row's own fact, re-derived per delivery, in a form where getting
  it wrong meant silently saying nothing. The service now reads `surface`/`room_id` once per
  runner connection and calls one `RoomSurface` port; `MatrixSystemPrompt`, `MatrixReplySink`
  and `MatrixProgressSink` collapsed into `MatrixSurface` with no filtering in it.
