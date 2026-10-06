# Codex app-server history APIs

Checked 2026-10-06 against the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server/)
and source at [`rust-v0.157.0`](https://github.com/openai/codex/tree/rust-v0.157.0), the current
runner and Bazel fixture pin. Use that selected release for recovery protocol work; do not assume
schema changes from newer CLI releases without updating the runner pin and fixture together.

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

## Recovery implications

The public API can replace private rollout parsing as the source of Codex's stored turn/item
projection, and it lets a client fetch that history in bounded pages. For history inspection alone,
prefer `thread/read` without turns followed by the paginated APIs; reserve `thread/resume` for the
separate need to attach a live Codex session.

This is not by itself a completion guarantee for an external side effect. A missing or unsupported
item page, partial pagination, or a Codex item that does not prove the corresponding runner action
outcome must remain unknown. Since the public API exposes a projection, absence from a returned page
cannot prove that raw model context or rollout records lack an item. Reconciliation still needs to
preserve command provenance, compare stable item identity and terminal state only where the protocol
exposes it, and never replay a prior side effect. In particular, the app-server protocol describes
Codex history; it does not replace Agentplane's canonical Action/Event evidence.

## Sources

- [Codex app-server docs](https://learn.chatgpt.com/docs/app-server/): experimental API negotiation,
  `thread/read`, turn/item pagination, and store support.
- [0.157.0 request registry](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server-protocol/src/protocol/common.rs#L817-L827)
  and [timeline registration](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server-protocol/src/protocol/common.rs#L1051-L1056):
  history methods and experimental annotation.
- [0.157.0 resume and history schemas](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L383-L472)
  and [turn/item/timeline schemas](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1604-L1742):
  resume exclusion, opaque cursors, timestamps, and timeline entries.
- [0.157.0 pagination handlers](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server/src/request_processors/thread_processor.rs#L846-L872)
  and [item-list handler](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/app-server/src/request_processors/thread_processor.rs#L3230-L3293):
  bounds, store support, and response projection.
  This note records the current protocol baseline for [`CODEX_RECOVERY_PROTOCOL`](../plans/task_dag.md#codex_recovery_protocol);
  it does not implement the protocol migration.
