# Claude Code: input queueing, acknowledgement, and withdrawal

Status: **provider evidence**, not a neutral API proposal.

[`common_protocol.md`](../docs/common_protocol.md) leaves one question open: whether Claude has an
enqueued/dequeued state that `join`/steer alone cannot express, and therefore whether the seam's
"one `Input` verb and no steer verb" rule is still right. It does. This page records what Claude
actually offers, in the shape a later side-by-side with Codex's `thread/queue/*` needs.

## Provenance

The normative source here is the **published type declarations** in
`@anthropic-ai/claude-agent-sdk` (`sdk.d.ts`), which document these frames and their semantics
directly; the notes below quote them. Frame _shapes_ not named as a type there were read off the
harness. Per the [shared rules](provider_protocols.md#shared-rules), none of this is pinned in a
scripted test yet: **a live probe must confirm each behavior before a driver depends on it.** The
coalescing rule in particular has a counter-intuitive outcome that deserves its own capture.

## The model in one paragraph

An input is not a turn. Each inbound user message may carry a `uuid`, and that uuid is a **handle
on a queued command**, not on a turn. The harness admits the message to a command queue, drains
the queue into turns on its own schedule, and reports transitions on that handle back over the
stream. So the states a caller can observe are queue states, and the operations available are
queue operations — which is precisely the shape the current seam has no vocabulary for.

## Acknowledgement

Lifecycle transitions come back as an outbound stream frame:

```json
{ "type": "command_lifecycle", "command_uuid": "<the uuid you sent>",
  "state": "started" | "completed" | "cancelled",
  "uuid": "<fresh uuid for this frame>", "session_id": "..." }
```

- `command_uuid` echoes the caller's uuid; that is the whole correlation mechanism.
- The frame is itself a message with its own `uuid` — do not confuse the two fields.
- **The input content is never echoed back.** There is no "here is what I received" frame; the
  handle is the only receipt.
- Forwarding is idempotent, and a remote transport suppresses it because it reports lifecycle on
  its own channel. A driver must not assume acks are present merely because it sent a uuid.

## Withdrawal

Two distinct operations, with different reach:

**1. `cancel_async_message`** — the targeted one. Documented as: _"Drops a pending async user
message from the command queue by uuid. No-op if already dequeued for execution."_

```json
{ "type": "control_request", "request": { "subtype": "cancel_async_message", "message_uuid": "<uuid>" } }
```

The response reports `{ "cancelled": <bool> }`. It is **best-effort against the queue, not an
interrupt**: an already-running turn is never pulled. There is a race guard — a cancel that
arrives before its target marks the uuid cancel-pending, so a message admitted later under that
uuid is dropped on admission rather than slipping through.

**2. `interrupt` with `cancel_queued: true`** — the broad one. Aborts the running turn _and_
sweeps the queue, closing each swept uuid with a terminal `cancelled` lifecycle and listing them
under the response's `cancelled` field. A plain `interrupt` does the opposite: queued commands
**survive**, and the response lists them under `still_queued` — uuids that _will_ run unless
cancelled first.

That `still_queued` receipt is the piece with no analogue in the seam today: it is the harness
telling the caller exactly which of its sends are now orphaned by an abort.

## The coalescing hazard

The harness does not run one queued command per turn. It pulls a run of compatible consecutive
prompts and merges them into a single turn, and the merged command carries **one representative
uuid** — the last contributor. The SDK states the consequence plainly:

> Cancellation granularity: uuids still in the queue are individually cancellable via
> `cancel_async_message`; once a batch is dequeued and coalesced into one turn, cancelling a
> NON-representative member uuid is a no-op (its content still runs), while cancelling the
> batch-representative uuid drops the WHOLE coalesced batch — in both cases the cancel response
> reports `cancelled:false` because the message was no longer in the queue.

Three things a neutral facade must not paper over:

1. `cancelled:false` is **ambiguous**. It means "not withdrawn from the queue", and covers both
   "already running, nothing happened" and "dropped an entire batch including inputs you did not
   name". A caller cannot distinguish these from the response alone.
2. Withdrawal is **not per-input** once coalescing has happened. The unit of withdrawal silently
   becomes the batch.
3. Inputs merged as non-representative members get **no terminal ack of their own**, because the
   dispatched command no longer carries their uuid. A driver that waits for `completed` per sent
   uuid will hang on them.

Compatibility gate: only prompts agreeing on mode, workload, meta/query flags, priority and
origin coalesce, and prompts carrying inlined images or relay rows never do. So a caller can
avoid the hazard by making consecutive sends deliberately incompatible — but that is a
workaround, not a contract.

## Capability negotiation

Claude advertises an **open set** of protocol capabilities on the `system`/`init` event, so a
driver should feature-detect rather than version-sniff. Relevant here:

| Capability                   | Meaning                                              |
| ---------------------------- | ---------------------------------------------------- |
| `interrupt_receipt_v1`       | the interrupt response carries `still_queued`        |
| `interrupt_cancel_queued_v1` | the interrupt request honors `cancel_queued: true`   |
| `queued_notifications`       | the CLI accepts inbound `queued_notification` frames |

Older CLIs simply resolve `interrupt()` to `undefined`. Absence of a capability is the supported
signal; it is not an error. This is a better fit for the seam's "never present an unsupported
operation as successful" rule than a version table would be.

## Against Codex, for a later common facade

Set next to Codex's durable `thread/queue/{add,list,update,delete,reorder,start}`
([`provider_protocols.md`](provider_protocols.md)), the two are closer than
`common_protocol.md` currently assumes — both have a real enqueued state and a withdraw
operation — but they are **not** the same object:

|                             | Claude                                                           | Codex                                                                                  |
| --------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Enqueue                     | implicit: every uuid-stamped input is queued                     | explicit `queue/add`                                                                   |
| Withdraw                    | `cancel_async_message` by uuid                                   | `queue/delete`                                                                         |
| Withdraw is race-free       | no — best-effort, with a cancel-pending guard for the early race | yes — mutex-serialized against dispatch, reports whether it actually removed something |
| Enumerate the queue         | only indirectly, via `still_queued` on an interrupt receipt      | `queue/list`                                                                           |
| Reorder / update            | not exposed                                                      | `queue/reorder`, `queue/update`                                                        |
| Relation to the active turn | drains into the current/imminent turn; **coalesces**             | never touches the active turn; starts a new one when idle                              |
| Unit of withdrawal          | the input, until coalescing makes it the batch                   | the queued item                                                                        |

The load-bearing asymmetry is the last two rows. Codex's queue is a durable list _beside_ the
turn; Claude's is a staging buffer _feeding_ the turn, and its entries can merge. A facade that
exposes "withdraw this input" over both would be honest on Codex and misleading on Claude the
moment a batch forms.

A common facade therefore looks plausible for **enqueue + withdraw-by-handle + terminal
acknowledgement**, provided it:

- treats withdrawal as **best-effort with a reported outcome**, never as a guarantee, since only
  one side can be race-free;
- carries a distinguishable "already dispatched" outcome rather than Claude's ambiguous
  `cancelled:false`, or leaves the ambiguity visible instead of inventing a state machine;
- does **not** promise per-input terminal acks, because coalescing legitimately retires several
  handles into one;
- keeps `list` / `reorder` / `update` out of the common surface, or gates them behind an
  explicit capability, since Claude has no equivalent.

Enumerating the queue and reordering it should stay provider-native until Claude demonstrates an
equivalent — per the existing rule that a related operation on both sides is not evidence the two
are equivalent.

## What to capture before pinning any of this

1. Two sends during an active turn: confirm both are queued, and whether they coalesce.
2. `cancel_async_message` against (a) a queued uuid, (b) a running uuid, (c) an unknown uuid —
   record `cancelled` and which lifecycle frames arrive.
3. The coalescing case explicitly: cancel a non-representative member, then a representative one,
   and record what runs and what acks.
4. `interrupt` with and without `cancel_queued`, recording `still_queued` and `cancelled`.
5. A CLI old enough to omit the capabilities, to confirm the absent-capability path.
