# Repeated content in staging History

Read-only measurements on the `agentplane-staging` PostgreSQL replica, 2026-11-12,
approximately 10:00 UTC. History still contains old sampler evidence after the
[sampler reset and native-content cutover](../../central_sampler/debug/2026_11_12_native_storage_cutover.md).
That reset did not rewrite or clear History.

## Repeated content across hosted-harness logs and projections

A broader read-only scan at approximately 10:22 UTC fingerprinted content inside **3,537,740
native-event, semantic-event and projection rows**, including both Codex and Claude native frames.
It compared decoded text values and selected JSON subobjects, rather than complete envelopes whose
IDs/timestamps differ. The examples below are exact equal content values, at least 1 KB each.
This scan excluded sampler boundary records, whose older request encoding is measured separately
below. It did not measure small values, arbitrary substrings or all possible near duplicates.

| Content                                    | Bytes per piece | Distinct stored rows | Scope                                           |
| ------------------------------------------ | --------------: | -------------------: | ----------------------------------------------- |
| One complete Codex turn diff               |          31,473 |                  473 | One Session, native `turn/diff/updated`         |
| Another complete turn diff                 |          89,288 |                  204 | Two Sessions, native `turn/diff/updated`        |
| One unchanged file patch within turn diffs |           2,007 |                  864 | Two Sessions, native `turn/diff/updated`        |
| Another unchanged file patch               |           7,492 |                  816 | Two Sessions, native `turn/diff/updated`        |
| One tool output body                       |           2,805 |                  719 | Eight Sessions, native/semantic/projection rows |
| Another tool output body                   |           2,833 |                  500 | Nine Sessions, native/semantic/projection rows  |

The 2,805-byte output appears in 332 native rows, 200 semantic rows and 187 projection chunk
rows. The 2,833-byte output has 501 field occurrences across 500 rows: one row holds it twice.
Repeated successful calls can legitimately have the same result; the counts measure stored
content repetition, not proof that the calls themselves should have been suppressed.

A focused scan found **31,322 Codex `turn/diff/updated` notifications**, totaling 888,603,520
protobuf bytes and **311,996,363 stored payload bytes (297.5 MiB)**, excluding row/index overhead.
Changing envelope fields do not prevent these notifications from repeating the same diff text.

- Complete diff strings of at least 1 KB: 30,796 occurrences, 2,609 unique values;
  **840,033,808 logical bytes versus 77,342,570 distinct bytes**. Exact repeats account for 90.8%.
- File patches split at unified-diff file boundaries, at least 1 KB: 174,400 occurrences,
  3,296 unique values; **798,229,359 logical bytes versus 19,717,532 distinct bytes**.
  Exact repeats account for 97.5%. This also finds unchanged files inside otherwise changing diffs.

These are alternative views of overlapping content; their byte totals must not be added. Logical
deduplication percentages do not predict the physical saving after PostgreSQL compression,
content-reference overhead or a concrete migration.

The [Session receiver](../../env/runtime/session/native.go) records every incoming native frame
before calling the adapter. The current Codex adapter has no semantic handler for
`turn/diff/updated`; the full frame remains retained evidence. History stores it at its cursor,
and its [delta settlement](../storage/settlement.go) handles specific text/argument/output delta
templates, not repeated diff snapshots. This path has no content-addressed sharing between events.

Separately, exact equal projection chunks of at least 1 KB of encoded JSON text accounted for
7,186,940 repeated stored text bytes out of 212,803,128 selected stored text bytes. That measures
whole-chunk equality only; it excludes overlaps with raw events and repeated portions inside
different chunks. The native diff stream is a larger confirmed target than this particular
within-projection whole-chunk duplication measurement.

## Turn-diff cleanup

At 11:13 UTC on 2026-11-12, the operator authorized deleting existing turn diffs without a
backup. A generated-protobuf decoder and JSON method check selected **31,322 incoming Codex
`turn/diff/updated` entries across 30 Sessions**; ten other entries mentioning that string
were excluded. The deleted payloads occupied **311,996,363 stored bytes (297.5 MiB)**.

Each Session transaction deleted only the selected native entries and added their removed-cursor
ranges; Session watermarks and other events stayed intact. History page and sequence reads
recognized the removed cursors for all 30 Sessions. A complete post-cleanup scan found zero
remaining turn-diff notifications. Ordinary `VACUUM ANALYZE` completed, making dead space reusable;
the event relation remained approximately 5.11 GiB, rather than shrinking its files. Both History
replicas remained available and CNPG remained ready. No backup was created for this cleanup.

The default-off notification control is tracked separately in
[PR #15862](https://github.com/agentydragon/ducktape/pull/15862); this cleanup did not deploy it.

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
