# Codex app-server history APIs

Checked 2026-10-06 against the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server/) and the protocol sources pinned by Agentplane's native protocol roster (Codex 0.152.0). A staging sandbox was observed running Codex 0.156.1 on 2026-09-27, so the repository's 0.152.0 wire fixtures are not proof of every deployed runner version.

## Documented read paths

- `thread/read` reads a stored thread without resuming it or subscribing to events. With `includeTurns` omitted or false it returns the summary; with it true it returns full turns. Full-history reads are deprecated for paginated threads.
- `thread/resume` is a live lifecycle operation. `excludeTurns: true` returns thread metadata and resume state without populating `thread.turns`. Codex still resumes the persisted conversation for the next model request; this only keeps the transcript out of the resume response. The pinned 0.152.0 protocol has this field without an experimental annotation.
- `thread/turns/list` pages stored turns with `threadId`, optional opaque `cursor` and `limit`, `sortDirection`, and `itemsView`. Pages contain `data`, `nextCursor`, and `backwardsCursor`; ordering defaults to newest first. `itemsView` selects omitted, summarized (the default), or full item data.
- `thread/items/list` pages persisted items, optionally scoped to `turnId`, with `cursor`, `limit`, and `sortDirection`; ordering defaults to oldest first. Pages contain `data`, `nextCursor`, and `backwardsCursor`. The 0.152.0/0.156.1 entries pair each projected item with its `turnId`.

There is a documentation/source mismatch to resolve before implementation. The current public docs classify both list methods as experimental and say `thread/items/list` can fail with an unsupported-method error when the active thread store does not implement item pagination. Yet the 0.152.0 and 0.156.1 request registries and parameter types do not mark either list method experimental, and both schemas expose them without a capability annotation. `thread/start.historyMode` and `thread/resume.initialTurnsPage` are explicitly capability-gated in those sources; `excludeTurns` is not. The docs also contain a separate paragraph saying paginated history operations fail closed, which conflicts with their turn/item API descriptions. Use the release-pinned protocol sources for the wire contract, and probe the exact runner binary/store to establish runtime support; do not add `experimentalApi` solely on the basis of the current docs.

In 0.152.0 and 0.156.1, the parameter schemas leave page size uncapped; the implementations default to 25 and clamp requests to 1–100. Treat this as observed implementation behavior, not a guarantee in the public schema. A `ThreadItem` is Codex's persisted display projection, not a raw Responses API `response_item` journal record; `itemsView: "full"` means full projected `ThreadItem` values available from app-server history.

The current upstream schema has moved on: it accepts a typed item-anchor cursor (scoped to a turn) and adds optional item timestamps. Those fields are absent from the 0.152.0 and 0.156.1 schemas checked here, so they must not be assumed by clients supporting those versions.

## Recovery implications

The public API can replace private rollout parsing as the source of Codex's stored turn/item projection, and it lets a client fetch that history in bounded pages. For history inspection alone, prefer `thread/read` without turns followed by the paginated APIs; reserve `thread/resume` for the separate need to attach a live Codex session.

This is not by itself a completion guarantee for an external side effect. A missing or unsupported item page, partial pagination, or a Codex item that does not prove the corresponding runner action outcome must remain unknown. Since the public API exposes a projection, absence from a returned page cannot prove that raw model context or rollout records lack an item. Reconciliation still needs to preserve command provenance, compare stable item identity and terminal state only where the protocol exposes it, and never replay a prior side effect. In particular, the app-server protocol describes Codex history; it does not replace Agentplane's canonical Action/Event evidence.

## Sources

- [Codex app-server docs](https://learn.chatgpt.com/docs/app-server/): experimental API negotiation, `thread/read`, turn/item pagination, and store support.
- [Codex 0.152.0 request registry](https://github.com/openai/codex/blob/rust-v0.152.0/codex-rs/app-server-protocol/src/protocol/common.rs#L751-L772), [resume fields](https://github.com/openai/codex/blob/rust-v0.152.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L383-L445), and [turn/item list schemas](https://github.com/openai/codex/blob/rust-v0.152.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1588-L1654).
- [Codex 0.156.1 request registry](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server-protocol/src/protocol/common.rs#L815-L836), [resume fields](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L404-L471), and [turn/item list schemas](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1604-L1670), from a version observed in a staging sandbox.
- [Codex 0.156.1 pagination implementation](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server/src/request_processors/thread_processor.rs#L2881-L2935) and [item-store dispatch](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server/src/request_processors/thread_processor.rs#L3230-L3286): page bounds and store-dependent support.
- [Codex 0.156.1 item protocol](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/app-server-protocol/src/protocol/v2/item.rs#L221-L356): item data is a tagged display union.
- [Current upstream item-list params](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1615-L1710): forward schema drift for anchor cursors and item timestamps.
- [Codex app-server thread-history tests](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_read.rs): upstream test coverage for paginated history.

This note records the investigation for [`CODEX_RECOVERY_PROTOCOL`](../plans/task_dag.md#codex_recovery_protocol); it does not implement the protocol migration.
