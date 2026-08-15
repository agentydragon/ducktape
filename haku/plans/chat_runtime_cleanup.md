# Chat runtime — what is wrong, and the order to fix it

Three sources feed this: a design review of `haku/console/x/` and its schema, a second review that
read the code de novo once the first's findings had landed, and a live investigation into sessions
that record nothing but a container boot and a death
(<../console/debug/2026_08_13_sessions_boot_and_die.md>). Items are deleted from here as they land,
so everything below is work that has not been done.

Two ambitions shape the ordering, both stated by the owner and neither of them a demand to
generalize now: the runtime should keep a sandbox across a console roll, and it is
single-Claude-CLI, single-Matrix-room by construction where both are eventually wanted plural. They
matter here because several pieces of obvious tidying are the _wrong_ tidying if done without them
in view, which is said at each such point.

## How the stages are ordered

Two constraints still bind, and everything else is preference:

1. **The frontend seam before the backend seam.** The first is smaller, is pure refactoring, and
   does not touch the schema until its last step; the second changes where meaning is extracted.
2. **Contract halves land when their roll converges**, independently of all of this.

Four are discharged: version negotiation preceded the field stage 4 added, diagnosis came before
change, a roll is survived before the TTL that was recycling wedged sessions is removed, and a
session survives being asked for before it is allocated on demand — which is why stages 5 and 6 sit
where they do. Both rest on stages 1 and 4, which have landed. The production verification that was
owed has since happened: deliberate console rolls on 2026-08-15 showed a session re-adopting across
each one, which surfaced two real holes the tests had not — the sweep beating the runner's redial,
and a graceful shutdown never releasing its leases — both now fixed
(<../console/debug/2026_08_13_sessions_boot_and_die.md>). Stage 4 raised the stakes on that rather
than lowering them — a turn is now inherited across a roll instead of being closed, so a roll is the
only thing that exercises it.

## Stage 5 — let the console own the lifecycle

_Both of its prerequisites, stages 1 and 4, have landed, and the stage is largely discharged — what
remains is the protocol-compatibility horizon._

`shutdownTime` was set to `now + session_ttl_seconds` at claim creation and never patched, so it was
not an idle timeout: a conversation in full flow died at exactly two hours, mid-turn, and the room was
told it failed (session 489b6f8e, measured 2026-08-15).

- ~~**Make the deadline a renewed lease, not a fixed timer.**~~ Landed. The console slides the
  SandboxClaim's `shutdownTime` forward on the same heartbeat that renews the console lease
  (`sandbox_claims.py` `renew`, hooked into `_renew_lease`), copying `sandbox_mcp`'s resourceVersion-
  guarded `_renew`. The deadline was kept rather than dropped — deleting it removes the only thing
  that reclaims a 2-vCPU sandbox when the console is not there to, which is exactly when it must not
  be pinned (matrix_chat_runtime.md R3.2a). The console Role gained `patch`. **And the janitor for
  these sandboxes is gone**: `haku-claude-workspace-janitor` reaped by `creationTimestamp` at 24h,
  which a slid deadline could not survive — a creation-age fence caps a tended session however far
  out its deadline is. With the console sliding the deadline and deleting the claim on a clean end,
  the controller's own `shutdownTime` is the reaper; the backstop was removed rather than re-keyed
  (operator, 2026-08-15). The only thing given up is a catch for the Agent Sandbox controller itself
  being broken for a long stretch.
- **Keep a horizon, because it is the protocol-compatibility window.** A runner's image is fixed at
  claim creation, so the oldest live runner is now as old as the longest continuously-tended session
  — and with no janitor ceiling, that is bounded only by when the session ends. That is how far back
  the console must still speak the bridge protocol; pick the range deliberately. _(Still open — the
  slide made the window real, and removing the janitor made it open-ended.)_
- ~~**An expired lease should mean unowned, not dead.**~~ Landed ahead of the rest of this stage: a
  production roll showed the sweep beating the runner's redial every time, because `release_lease`
  is a finalizer and never runs. `expire_stale_leases` now waits an `ADOPTION_GRACE` past expiry, a
  graceful shutdown releases every held lease, and the failure message says which of the three things
  actually happened (<../console/debug/2026_08_13_sessions_boot_and_die.md>).

**Done when** no session dies on a clock — reached; and the bridge-protocol support range is derived
from the horizon rather than assumed — open.

## Stage 6 — allocate a sandbox because there is something to do

_Needs stages 1 and 5 (constraint 3)._

An idle room holds a sandbox permanently: the supervisor provisions whenever the room has no live
session, the warm pool is `replicas: 0` so every claim is a cold start, and the cycle repeats on the
TTL — twelve cold starts a day for a room nobody speaks in, each announcing itself and narrating its
bootstrap there, holding ~1 CPU / 2Gi of an 8 CPU / 16Gi quota in between.

The SPA has a gesture that means "I want a session" and Matrix has none, so the supervisor
substitutes by assuming demand permanently. The prompt is the honest substitute, and the prompt
queue already made it cheap: accepted and running were separated when `claude_chat_prompts` landed.

- `create()` writes the row and stops, in a new **`idle`** status; `allocate()` mints the credential
  and the claim and moves to `provisioning`.
- Admission accepts on `idle`, so `enqueue_prompt` is what creates demand; the supervisor's trigger
  becomes "an unclaimed prompt and no sandbox", waking on `ChatEventKind.PROMPT`.
- `MatrixTurns.offer` stops refusing an unallocated session, so the batch enters the durable queue
  rather than being left on the homeserver.
- **`LIVE_SESSION_STATUSES` currently means both "worth keeping" and "has a lease to renew".** An
  idle session is the first that is worth keeping with no holder to lose; split the set rather than
  giving it a fake far-future lease.
- **Adding an enum value is two releases here**, not additive: `TextBackedStrEnumColumn` parses the
  column, so a replica on the previous image reading `idle` fails rather than degrading. Widen the
  member and the CHECK first; write it next release.

**Cost, stated plainly:** the first message after quiet pays the full cold start. Measure it rather
than assume it away.

**Done when** an idle room holds no sandbox and the first message provisions one.

## Stage 7 — the two seams

_Everything above is possible without these; they are what stop the next feature from being a
rewrite._

### The frontend seam — first, and mostly refactoring

`RoomSurface` takes `room_id` on all six methods and `MatrixSurface` opens five of them with
`del room_id`, because its channel is already bound to the one room. So the port reads as multi-room
and behaves as one-room-globally, and `room_id: str | None` threads through the turn loop making four
call sites re-ask a question answered once per connection — plus three no-op coroutines so
`_TurnStatus` has something to call.

1. A `ChatFrontend` port with **no address parameter**: a null implementation for the SPA, which
   needs none of it because its client reads the message rows, and `MatrixSurface` bound at
   construction as it effectively already is. Every `or room_id is None` and all three no-ops go.
2. Then the schema half — `chat_attachment(session_id, surface, address, attached_at, detached_at)`
   with a partial unique index on `(surface, address) where detached_at is null`. It subsumes
   `claude_chat_sessions.surface`, `.room_id`, both check constraints tying them together, and
   `matrix_conversation.session_id`; the pointer/history distinction those two tables document in
   prose becomes `detached_at IS NULL`. Attach/detach within one session becomes a row.

### The backend seam

Launch and transport are already a package boundary, the transcript's shape is already neutral, and
the frontends are a different question. What is welded is **meaning**: `_run_turn` matches on the
CLI's own frame `type` and six helpers parse Anthropic content blocks, so the turn loop is a frame
interpreter. Every branch of it reduces to five events naming nothing provider-specific —
`TextDelta`, `MessageCompleted(id, text, calls)`, `ToolResult`, `Activity`,
`TurnCompleted(outcome, text, cost, usage, duration)` — with one adapter per backend producing them.
`ClaudeCli.frames()` already sits where that adapter goes; it yields wire dicts, which is why the
console became the interpreter.

Two corrections this forces on items that would otherwise look like cleanups:

- **`tool_uses` must be deleted into a `tool_call` table**, written by the adapter — not into a parse
  of Claude frames, which only sessions on that backend have. It answers the lossy-copy objection by
  holding the result, and it is where the rollout join lands anyway.
- **"Projection or primary" is settled against the projection.** The rollout is one protocol's wire
  and cannot be the record a second backend shares. Transcript primary, rollout per-backend evidence
  — so what to remove from the duplication is the double write and `agent_message_id`.

**Done when** a second frontend and a second backend are representable without touching the turn
loop's signatures.

## Anytime — independent of the stages

- **Every stream delta re-reads the whole session.** `update_assistant` NOTIFYs per delta →
  `_sse_stream` wakes → `store.get` → `_rollout_calls` selects every `assistant`/`user` frame and
  re-parses its content blocks. O(session) per token batch, paid only while the SPA is streaming.
  Fix by indexing incrementally on the agent message id, or by scoping the read to the messages
  asked about.
- **Split `claude_chat.py`** further. `KubernetesSandboxClaims` has left; what remains is view
  models, the store, the recorder, the status driver, the service, the turn loop, the port and the
  routes. `ClaudeChatStore`'s twenty-odd methods split along the seams the turn table and the
  prompt queue already created — each split landing with the change that creates it, never as a
  standalone reshuffle with no acceptance criterion.
- **Prune the archaeology.** Roughly 150 of `claude_chat.py`'s lines narrate what the code used to be
  and which bug that caused; STYLE puts those under Remove. Keep the invariant as one imperative
  line ("do not gate on session status: admission asks the turn"), move the story to `debug/`.
- **`bridge_token_fingerprint = b""`** is a cleanup-pending tri-state on a credential column, and the
  zero value STYLE says never to use for absence. It wants to be `claim_cleaned_at`.
- Smaller: `list_turns`/`read_frames` take `str` session ids and parse them, where the boundary is
  the MCP tool; the store imports its read models from the MCP tool that reads it;
  `_message_view`'s `_NO_CALLS` default serves one caller that structurally cannot need it;
  `KubernetesSandboxClaims._clients()` is double-checked locking with four asserts;
  `MatrixTurns.offer` pre-checks the status that `enqueue_prompt` then checks atomically, and its own
  comment admits it races; `claude_chat_turn_prompts` has no reader until folding lands; "surface"
  names five things and "turn" three.

## When their rolls converge

- **The prompt queue's compatibility half.** The transcript row is still minted `pending` and the
  `_legacy_pending` scan still answers a prompt an old replica accepted. Both tombstoned. Write the
  row final and drop the scan; `'pending'` stays in the CHECK constraint.
- **`tool_uses`.** Server default first (`SET DEFAULT '[]'::jsonb`), then stop writing it, then
  `drop_column` in a third release — an old replica's `_message_view` selects the mapped column by
  name. Into the `tool_call` table of stage 7, not into a frame parse.

## Later

- **Mid-turn steering.** Measured to work: a prompt written while a turn runs is absorbed at the next
  tool boundary and one `result` covers both. `claude_chat_turn_prompts` is many-to-one already and
  admission deliberately still refuses, with a test saying so. Needs `interrupt` with
  `cancel_queued: true`, since a bare interrupt starts the next queued prompt.
- **Streaming the answer into the room.** Achievable only as a coarse refresh — the five-second floor
  is Synapse's rate limit and what a person can read, and every edit is a permanent federated event.
  Stream into the status line's mechanism (a notice, edited, redacted at the end) rather than editing
  the reply, because editing the reply puts partial drafts into the tail `recent_messages` reads and
  the msgtype trick that excludes chatter does not apply to an `m.text` edit.
- **Source the CLI from npm** rather than out of the Agent SDK wheel
  (<cli_protocol_ownership.md>).
