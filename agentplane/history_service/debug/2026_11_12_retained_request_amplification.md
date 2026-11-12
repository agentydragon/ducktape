# Retained request amplification in staging History

Read-only measurements on the `agentplane-staging` PostgreSQL replica, 2026-11-12,
approximately 10:00 UTC. History still contains old sampler evidence after the
[sampler reset and native-content cutover](../../central_sampler/debug/2026_11_12_native_storage_cutover.md).
That reset did not rewrite or clear History.

## Old sampler request snapshots

Across `session_event`, boundary records (kind 31) occupy 1,834,786,518 stored payload bytes;
native protocol events (kind 20) occupy 1,605,258,498. These are sums of
`pg_column_size(payload)`, excluding row/index overhead and unused relation space. They are not
physical relation totals or measurements of duplication by themselves.

The largest retained sampler Session, `c272af29-0acd-4ad8-a6d9-6941d3c2ebe7`, has
1,057,133,036 stored event-payload bytes. Its sampling-dispatch evidence alone accounts for
**984,351,175 bytes (938.8 MiB)** across 1,586 fragments, with 550 request starts identified by
the inline `body` field. Dispatch labels were selected from the protobuf prefix; six samples
distributed through the cursor range were decoded with generated protobuf types and their
fragments reassembled by record ID.

| Context items | Entire request-evidence JSON bytes | Stored fragment bytes |
| ------------: | ---------------------------------: | --------------------: |
|             2 |                             83,114 |                41,495 |
|           215 |                            778,653 |               447,929 |
|           418 |                          1,632,256 |               982,248 |
|           635 |                          2,488,838 |             1,952,711 |
|           822 |                          3,330,738 |             2,440,825 |
|         1,026 |                          4,191,565 |             2,943,701 |

Every sampled request contains both structured `body` and a full `wire_request` string. Parsing
the latter produces the same JSON structure as `body`. The evidence therefore preserves both
the repeated growing context and its redundant encoding. Retaining successive growing snapshots
has the same quadratic-shaped accumulation measured in the old sampler; this sample does not
establish that every History producer grows quadratically.

History [appends the serialized EventEntry](../storage/store.go) at each new cursor and checks
exact replays at old cursors. It does not deduplicate equivalent payload content at different
cursors. The new sampler format stops creating these old inline request records; it does not
reclaim the historical copies already in History.

## Native harness snapshots

Two native Codex `thread/resume` replies in Session `06576486-f35b-425e-989d-2af95036f027`, cursors 148663
and 148677, each contain **14,222,637 JSON bytes**, including the same 117-turn history.
They were recorded on October 5 and 6. Their result objects differ only in `thread.updatedAt`;
the turn arrays compare equal. Stored event payloads are 5,733,491 and 5,733,493 bytes.
The associated requests supplied only `threadId`, without `excludeTurns`. This proves another
repeated full-history representation, not that all native events or current resume paths have
quadratic growth. Current Codex adapter code requests `excludeTurns` on resume.

## Thread projections

The [projection writer](../projection/payloads.go) shares previous chunks when appending text,
and [entity writes](../projection/recording.go) update the current entity row. Completion with
the same text reuses the existing body. It does not retain a full thread snapshot per token.

Measured projection payloads had 685,913 chunk rows, with 296,434,982 stored JSON-text bytes
and 610,832,609 uncompressed JSON-text bytes. The 733,070 manifests sum to 5,103,919,133
`content_bytes`, but that number repeatedly counts shared prefixes and is not stored content.
All measured chunks belonged to one projection epoch, `history-v1`.

The projections still represent some message/tool content already present in raw events, and
their rows/indexes cost space. No equivalent full-context snapshot accumulation was established
in this path. Any cleanup or cross-service deduplication needs separate replay, evidence,
authorization and retention decisions; this investigation changed no History data.
