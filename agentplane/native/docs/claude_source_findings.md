# Claude Code source and capture findings

The [protocol roster](protocol_roster.md) and Claude harness test docstrings record the behavior
tested against the pinned CLI, Claude Code 2.1.252. This page retains a few useful findings that are
not covered by those tests. Each section labels its evidence; none of the source-only findings is an
Agentplane capability or a tested protocol guarantee.

## Queued-message withdrawal (source-only)

The tests cover compatible-input coalescing and the receipts for queued input around interrupts
([active-turn tests](../../native/claude/test_active_turn.py),
[resume test](../../native/claude/test_turns.py)). They do not exercise
`cancel_async_message` or cancellation after coalescing.

The published Claude Agent SDK type declarations describe `cancel_async_message` as a best-effort
removal by message UUID. Static inspection of the pinned 2.1.252 CLI bundle shows an early-cancel
marker for a later-arriving UUID and a coalesced batch represented by its last input UUID. After
dequeue, canceling a nonrepresentative UUID does not remove the batch; canceling its representative
can remove the whole batch. The SDK declaration's version is not tied to the CLI pin, and the
bundle behavior has not been captured or scripted-tested. The Agentplane native driver does not
expose this control.

## Capability advertisement (capture of 2.1.220)

An older live capture observed the CLI advertising capabilities on `system/init`; its
`initialize` request did not accept client capability declarations. The observed list included
`msg_lifecycle_v1`. This was not checked against the currently pinned 2.1.252 build. The published
SDK type declarations mention additional capability names but are not version-linked to the CLI
pin.

## Transcript writer (static inspection of 2.1.252)

The CLI serializes transcript appends and deduplicates rows by message UUID, but an append can
resolve after enqueueing and before the file write. Explicit flush, a terminal result, and orderly
shutdown are persistence fences. Recovery quarantines an incomplete JSONL tail and retains its
valid prefix. Compaction writes and syncs a temporary transcript, checks that the source stayed
unchanged, retains valid concurrently appended rows, and validates the resulting UUID/parent chain
before publishing it.

The runner contract relies only on the distinction between append acceptance and a persistence
fence ([Harness Host session protocol](../../env/harnesshost/docs/session_protocol.md#what-the-harnesses-do-not-promise)). The
recovery and compaction internals above are static findings, not tested guarantees.

## Auto-compaction frames (live capture of 2.1.233)

This older capture has not been reverified against the pinned 2.1.252 binary. With forced automatic
compaction, the observed order was `PreCompact`, `system/status: compacting`, `SessionStart` with
`source: compact`, `system/status` success, `system/compact_boundary`, then a synthetic `user` frame
containing the summary. The boundary carried accounting metadata but no summary text; the following
synthetic user frame carried that text. The [native TODO](../TODO.md#scripted-compaction-test) tracks
the remaining scripted check. The current
[manual-compaction test](../../native/claude/test_compaction.py) checks that `/compact`
retains the standing system prompt, not this automatic-compaction sequence.

## Interactive control identity (static inspection of 2.1.252)

The CLI uses `request_id` to correlate a live control exchange and `tool_use_id` to identify the
semantic permission request across recovery. Reinitialization returns pending control envelopes;
an unanswered human request can remain parked across shutdown. The runner currently answers
permission prompts and refuses unsupported controls rather than persisting parked requests. Any
future interactive-control work must persist the semantic identity and reconstruct pending
requests; callback/resolver objects are process-local.

## Rate limits and refusal fallback (static inspection of 2.1.252)

Low-priority API waiting and refusal fallback are separate paths. The former has distinct reset,
budget, eligibility, and maximum-wait exits. The latter can park for a user choice only when its
dialog kind was declared during initialization; otherwise it fails closed to the classic refusal
error. Neither behavior is covered by the scripted suite or translated into a typed runner event;
the runner retains these frames as native evidence.
