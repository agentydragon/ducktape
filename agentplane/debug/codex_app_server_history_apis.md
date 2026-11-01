# Codex app-server history APIs

Checked 2026-10-24 against the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server/)
and source at [`rust-v0.157.0`](https://github.com/openai/codex/tree/rust-v0.157.0), the current
runner and Bazel fixture pin. Use that selected release for recovery protocol work; do not assume
schema changes from newer CLI releases without updating the runner pin and fixture together.

## Verified omission, and its limit

The omission claim is **true for legacy history in the pinned 0.157.0 binary**. It is too broad
as a justification for private rollout parsing on every thread: new persisted threads on the local store default to
paginated history, which preserves the tested items and their identities.

[`test_history_projection_against_resumed_model_input`](../native/codex/test_history_projection.py)
starts the real pinned binary against the scripted Responses endpoint. It completes one turn with
reasoning A, a successful `exec_command`, reasoning B, and an assistant message. It reads every
page (page size 1), closes the process, reads from a fresh process before resume, resumes with
`excludeTurns: false`, and captures the next actual model request. It covers both an omitted
`historyMode` (asserted to select `paginated`) and explicit `legacy`.

| View after restart                                | Legacy history                                                                     | Default paginated history                                                          |
| ------------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `thread/read(includeTurns: true)`                 | Shell call absent; reasoning A/B merged into one synthetic-ID item                 | Both reasoning IDs/summaries, shell call/output and assistant ID preserved         |
| `thread/turns/list(itemsView: "full")`, all pages | Same omission and identity loss                                                    | Same tested items preserved                                                        |
| `thread/items/list`, all pages                    | Method not found, code -32601                                                      | Same tested items preserved                                                        |
| `thread/timeline/list`, all pages                 | Method not found, code -32601                                                      | Same tested items preserved                                                        |
| `thread/resume(excludeTurns: false)`              | Same omission and identity loss                                                    | Same tested items preserved                                                        |
| Next actual Responses request                     | Both original reasoning summaries/encrypted payloads and shell call/output present | Both original reasoning summaries/encrypted payloads and shell call/output present |

The same API differences are asserted while the original process is loaded and after resume.
The rollout retains each reasoning record under the ID announced by `item/completed`; the test
asserts those identities against the live notifications. This is not an inference from the word
"projection", an empty first page, or `excludeTurns: true`.

Run `bbr test //agentplane/native/codex:test_history_projection`. Both cases passed on
2026-10-24 ([BuildBuddy invocation](https://app.buildbuddy.io/invocation/b2e513cd-9c39-4811-8786-295e4db9e58e)).
Its undeclared outputs contain
`native/test_history_projection_against_resumed_model_input[None]/history-comparison.json` and
`...[legacy]/history-comparison.json`, plus the raw stdin/stdout traces for each process. The JSON
contains every history page, live completed items, rollout records, and the resumed model request.
It contains only scripted fixture data.

This verifies a completed shell call and two reasoning items across a clean process restart.
It does not establish equivalent continuation after interruption or process loss during a turn,
compaction outside the observed recovery window, external rollback, or for every tool kind. The public
reasoning item also omits `encrypted_content`; seeing a summary is not proof of the encrypted
continuation payload. The runner compares observed item content, so that field difference alone does
not establish that it needs private storage. The runner uses turn-scoped public history RPCs and
reports unknown when those RPCs cannot establish continuation; see the
[Harness Host recovery contract](../env/runtime/harnesshost/docs/session_protocol.md#interrupted-turns-and-continuation-recovery).

### Completed compaction

[`test_compaction_history_projection_against_resumed_model_input`](../native/codex/test_history_projection.py)
uses `thread/compact/start` on a persisted paginated thread with a reasoning item, a completed shell
call, and an assistant answer. It answers the compaction request with a distinctive summary, closes
the app-server after the compaction item completes, reads history from a fresh process before and
after `thread/resume`, and captures the next Responses request. The test artifact and raw stdio traces
are available from the
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/7d992d81-731a-4b55-ad42-8301e951081b).

Every public view tested (`thread/read`, `thread/turns/list`, `thread/items/list`, and
`thread/timeline/list`) retains all pre-compaction item IDs and the completed command output after
compaction and restart. Each adds the same `contextCompaction` ID. The views do not expose the summary
text. The actual next model request contains the original user message, the generated summary, and
the new user message; it omits the pre-compaction reasoning items, function call, function-call
output, and assistant answer. Public display history therefore retains identities that the next
model request no longer carries as continuation items. A compaction marker in the recovery window
does not establish that those earlier items remain in model context, which supports reporting the
window as unknown.

This evidence covers an explicitly completed local compaction followed by process replacement. It
does not cover interruption or process loss during compaction, compaction outside the observed
recovery window, automatic threshold compaction, or external rollback.

### Why the pinned source behaves this way

All source links below use immutable commit
[`00c972ed5d6ff6499317fd41b7f23605b8e6850d`](https://github.com/openai/codex/commit/00c972ed5d6ff6499317fd41b7f23605b8e6850d),
the target of tag `rust-v0.157.0`.

- [Persistence policy](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/rollout/src/policy.rs#L39-L63)
  saves raw reasoning, function calls and outputs. But legacy mode does not save completed
  `CommandExecution` items, and shell begin/end events are transient
  ([policy](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/rollout/src/policy.rs#L93-L175)).
- The [legacy history builder](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/thread_history.rs#L477-L496)
  ignores raw reasoning/function response records. Its
  [reasoning handler](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/thread_history.rs#L538-L577)
  assigns synthetic IDs and merges adjacent reasoning events; with the shell item missing,
  these two reasoning summaries become adjacent.
- [Default selection](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L1460-L1463)
  selects paginated history for a persisted thread when its store supports it. Paginated mode
  saves completed items, and its
  [projector](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/thread_history_projection.rs#L76-L102)
  preserves their IDs through `ThreadItem::from`.
- [Model-context loading](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/thread-store/src/local/model_context.rs#L35-L79)
  reads rollout history separately from these display items. The actual captured next request
  establishes which records survive into model input in this scenario.
- The [compaction implementation](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/core/src/compact.rs#L360-L368)
  builds replacement history from retained user messages and a summary; it installs that history
  with compaction metadata ([summary construction](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/core/src/compact.rs#L751-L759)).
  The public history builder records a `ContextCompaction` item with an ID
  ([handler](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/thread_history.rs#L1176-L1178));
  it does not place the summary text in that item.

## Documented read paths

- `thread/read` reads a stored thread without resuming it or subscribing to events. With
  `includeTurns` omitted or false it returns the summary; with it true it returns full turns. Full
  history reads are deprecated for paginated threads.
- `thread/resume` is a live lifecycle operation. `excludeTurns: true` returns thread metadata and
  resume state without populating `thread.turns`. Codex still resumes the persisted conversation
  for the next model request; this only keeps the transcript out of the resume response. The field
  is not experimental in 0.157.0.
- `thread/turns/list` pages stored turns with `threadId`, optional opaque `cursor` and `limit`,
  `sortDirection`, and `itemsView`. Pages contain `data`, `nextCursor`, and `backwardsCursor`;
  ordering defaults to newest first. `itemsView` selects omitted, summarized (the default), or full
  item data.
- `thread/items/list` pages persisted items, optionally scoped to `turnId`, with `cursor`, `limit`,
  and `sortDirection`; ordering defaults to oldest first. Each result pairs a projected item with
  its `turnId` and optional start/completion timestamps. Pages contain `data`, `nextCursor`, and
  `backwardsCursor`.
- `thread/timeline/list` is also present in 0.157.0. It is explicitly experimental and returns
  bounded pages of one ordered timeline containing items, realtime entries, and turn-start/turn-
  complete markers, with positions and a next cursor. Its handler delegates to the active thread
  store, so support depends on that store. This unified ordering may be a better reconciliation
  input than independently paging turns and items.

There is a public-docs/source mismatch to keep in mind before implementation. The public docs
classify `thread/turns/list` and `thread/items/list` as experimental and say item listing may fail
with an unsupported-method error when the active thread store lacks pagination. The 0.157.0 request
registry does not mark either list method experimental, but its item-list handler does return
method-not-found for an unsupported store. `thread/start.historyMode` and
`thread/resume.initialTurnsPage` are experimental; `excludeTurns` is not. The docs also contain a
separate paragraph saying paginated history operations fail closed, which conflicts with their
turn/item API descriptions. Use the selected release's protocol sources for the wire contract, and
verify the experimental handshake and exact store's runtime support; do not add `experimentalApi`
to methods solely on the basis of the current public docs.

The 0.157.0 list schemas leave page size uncapped; the implementation defaults to 25 and clamps
requests to 1–100. Treat that as implementation behavior, not a schema guarantee. A `ThreadItem` is
Codex's persisted display projection, not a raw Responses API `response_item` journal record;
`itemsView: "full"` means full projected `ThreadItem` values available from app-server history.
The 0.157.0 item-list cursor is an opaque string and entries carry optional timestamps.

## Runner regression coverage

`//agentplane/env/e2e:test_restart` runs the Go runner, the real pinned Codex binary and a scripted
model endpoint. The paginated recovery case includes 101 reasoning items, assistant text and a
completed shell command followed by a failed turn and process replacement. It checks retained
visible items, unchanged encrypted reasoning in the next model request, complete cursor traversal,
and native RPCs scoped to the latest completed turn plus later turns. An older completed turn is
present but must not be read. A live interruption case exercises reconciliation without a process
restart. `//agentplane/app:test_bridge` checks the materialized Thread and side effects across
ordinary interruption, shutdown and kill for both native harnesses; interrupted Codex commands
must remain explicitly unknown.

Go tests cover empty turns, cross-turn item ownership, shared resume snapshots, invalid/partial
page sequences, unsupported history modes/items, and compaction barriers. A scheduler regression
requires a history RPC response to pass through stdout while reconciliation waits, and checks that
later input cannot dispatch before the report. A separate command regression covers a native
`turn/start` response arriving before its `turn/started` notification.

## Recovery implications

The runner uses the public API as its Codex history source. `thread/start` and `thread/resume`
return the history mode; no separate metadata read or private file/database access is needed.
`thread/items/list(threadId, turnId)` reads only selected recovery turns and any original owning
turn of an item that finishes later. It follows all pages within those turns with limit 100,
shares the snapshot across a resume, and skips entirely unobserved windows. Later unobserved
turns are still checked for compaction barriers when validating earlier items. It does not page
through unrelated older turns. The runner checks the paginated contract and treats unsupported
or incomplete reads as unknown. Read-only RPCs execute off the stdout reader and gate further
input until reconciliation commits.

This is not by itself a completion guarantee for an external side effect. A missing or unsupported
item page, partial pagination, or a Codex item that does not prove the corresponding runner action
outcome must remain unknown. The legacy counterexample above establishes that even complete API
history can omit an item that survives in model input. The paginated case above does not show that
omission. Reconciliation still needs to preserve command provenance, compare stable item identity
and terminal state only where the protocol
exposes it, and never replay a prior side effect. In particular, the app-server protocol describes
Codex history; it does not replace Agentplane's canonical Action/Event evidence.

## Sources

- [Codex app-server docs](https://learn.chatgpt.com/docs/app-server/): experimental API negotiation,
  `thread/read`, turn/item pagination, and store support.
- [0.157.0 request registry](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/common.rs#L846-L862)
  and [timeline registration](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/common.rs#L1086-L1090):
  history methods and experimental annotation.
- [0.157.0 resume and history schemas](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L354-L435)
  and [turn/item/timeline schemas](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1705-L1840):
  resume exclusion, opaque cursors, timestamps, and timeline entries.
- [0.157.0 pagination handlers](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L3043-L3117)
  and [item-list handler](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L3402-L3460):
  bounds, store support, and response projection.
