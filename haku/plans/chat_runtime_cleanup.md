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

## 1. A turn exists

**Done.** `_run_turn`'s stack frame used to be the only place a turn existed, so everything that
needed to name one named something else: `ChatSessionStatus` carried `responding` beside the
session's own lifecycle and `update_assistant` re-asserted it **per stream delta**, `enqueue_prompt`
set it before any turn started — so `request_abort` accepted an abort for a turn that did not
exist — and a `result` frame's cost, usage and duration were read for an error check and dropped
for want of an owner.

**What a turn is**: one exchange, from the harness handing the agent a prompt to a final answer or
a failure. It contains many assistant messages, many tool uses, many model round trips. It is not
a model round trip (the CLI's own `num_turns` counts _those_, which live inside ours), not a
message, and not a Matrix message (R2.1 coalesces a batch into one prompt).

**Stored as a bracket, not a label.** A `turn_id` stamped on each frame would write our
interpretation into the record of the wire, and the wire does not agree with it: the CLI folds a
second prompt into a running turn (§2), so one `result` frame can cover two prompts. The turn row
records a **range** instead, so the log stays verbatim and re-bracketing later is an update to our
table rather than a rewrite of the record.

```text
claude_chat_turns(turn_id, session_id, first_frame_seq, last_frame_seq | None,
                  started_at, ended_at | None, outcome, cost_usd, usage, duration_ms)
claude_chat_turn_prompts(turn_id, message_id)   -- many prompts per turn once folding is used
```

What that bought, in the shape the runtime asks about it: `next_prompt` dequeues the prompt and
opens the turn in one transaction, `_run_turn` is that turn's span and closes it on every exit, a
partial unique index makes "at most one open turn per session" a schema property, and admission,
abort and the SPA's `responding` are all one lookup against it. `ended_at IS NULL` on a session
with no live holder is now exactly "abandoned mid-flight" — nothing ran to close it — where before
it was representable only as a lie: a live status nobody was maintaining.

Two things deliberately unchanged. The partial frame's per-session uniqueness stays: a session has
at most one open turn, so the existing index is already the stronger statement, and a per-turn
version would need `turn_id` on the frame row. And `responding` stays in the enum and in
`LIVE_SESSION_STATUSES` — a replica on the previous image still writes it, the lease sweep can only
reclaim such a row while it looks for that value, and the view derives from either source until the
roll converges.

**Not part of it: the reading API.** Phase 5 used to specify a `read_turn`, which made this a
prerequisite for reading past conversations. It was not one — a cursor over `frame_seq` with a
`kinds` filter gives the bounded drilldown that requirement wanted. `list_turns` reports the
brackets as an index into that log rather than reshaping it.

## 2. Mid-turn steering works and we are not using it

Measured, not inferred (<../cli_protocol/probes/steering.py>, 2026-08-12): a prompt written
to the CLI while a turn is running is **absorbed at the next tool boundary**, the model acts
on it, and one `result` frame covers both prompts. <matrix_chat_runtime.md> R2.2a defers this
as having "no native mechanism"; that is now corrected there.

Nothing on our side was preventing it either — `ClaudeSDKClient.query()` is a bare
`transport.write()` with no interlock. What prevents it is the shape of our loop:
`receive_response()` drains to `ResultMessage` before looking for the next prompt.

So `MatrixTurns.offer` can stop refusing batches during a turn (R2.2 becomes fold-into-turn)
and "actually, skip the calendar part" reaches Haku while it is working. §1 landed the shape this
needs — `claude_chat_turn_prompts` is many-to-one already — and deliberately did not turn it on:
admission still refuses a second prompt while a turn is open, and a test says so.

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

## 2a. `system/task_*` frames are a status line we already store and ignore

**Done.** `system/task_started` and `task_progress` carry `tool_use_id`, a `task_type` and a
human-readable `description` — "Running Count regular files in the directory" — which is R6's
"what is Haku doing right now" without inventing anything. The turn loop now derives a coarse
state from those and from each `assistant` frame's `tool_use` names, and the room shows it on a
single lazily-created, rate-limited, redacted-on-finish line.

What remains of R6 is R6.1: typing notifications for the turn's duration, cleared on every
terminal path.

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

## 5. Frame recording belongs on the protocol client

`RecordingWebSocket` decorates the socket, below the transport, and re-decodes each frame's
envelope to see what crossed — a second `json.loads` of every frame.

This item used to propose an `on_frame` callback on `WebSocketTransport`, beside the
`on_progress: ProgressSink` it already takes. That is no longer the best shape. `ClaudeCli` now
owns the reader and **already has each frame parsed** (§0), so recording there is one parse
instead of two and one place that knows the envelope instead of two — without the shared
transport learning about the console's database, which is what the decorator was written to
avoid in the first place.

## 6. The lease means two things and never says who holds it

**Done.** `lease_expires_at` is a creator-granted provisioning budget before a runner attaches
(`PROVISION_LEASE`, ten minutes) and an owner heartbeat afterwards (`LEASE_TTL`, ninety seconds); it
recorded _when_ but never _who_, so every session the sweep reclaimed produced the same sentence.
`lease_holder` carries the holder — `HOSTNAME`, which is what `kubectl logs` takes — and by being
NULL or not it also says which of the two lease kinds is running, so the column that had two
meanings now states which one it has. What is **not** done: <cli_protocol_ownership.md> wants an
expired lease to mean **unowned** (adoptable) rather than **dead**, and reinterpreting it before an
adopter exists would leave a room silent behind a healthy-looking row.

## 7. `ClaudeChatStore` is a god object

Twenty-odd methods across session lifecycle, prompt queue, transcript, frames, turns, leases and
claim-cleanup bookkeeping. It splits along the seams §1 and §3 create: sessions/leases, prompts,
transcript, rollout. Not as a PR of its own — a standalone reshuffle has no acceptance criterion
and would conflict with everything else here; each split lands with the change that creates its
seam.

## 7a. `agent_sdk_transport` is named after a dependency it no longer has

The package holds the bridge envelope, the websocket channel, the CLI protocol client and the
launch builder. None of it is an Agent SDK transport; the SDK is gone from the code, and what
remains of the wheel is a build-time source for the `claude` binary (§0's note on moving that to
npm). `runtime/x/claude_bridge` or similar would say what the package is.

Mechanical but not free: imports across the console and the runner, the Bazel target paths, the
`runner_image`/`runner_bin` labels, and whatever in `cluster/` names them. The same applies to
`HAKU_AGENT_SDK_RUNNER_TOKEN`, which is a deploy contract — a Secret key and env var in eight
places — so renaming that one wants a two-step expand/contract rather than a sweep.

## 8. Smaller, mechanical

- **Done:** `abort_session` reached into `service._store` for an ownership check and then asked the
  service for the abort. `ClaudeChatService.request_abort` now takes the Operator and raises
  `KeyError` for a session they do not own, so the route asks one question.
- **Done:** `_sse_stream` compared `model_dump_json()` strings — three serializations of a view that
  embeds the whole transcript, per wake. It is one now, compared against what was last sent. It
  still suppresses little during a turn, which is the reason not to pay for it three times: every
  delta genuinely changes the view.
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
