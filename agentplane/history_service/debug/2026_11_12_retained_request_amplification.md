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

## Completed reasoning blocked by stderr

A read-only trace at approximately 11:39 UTC found a completed Codex reasoning item in Session
`58e5d7cc-8e45-46e8-bf7b-8e8a79b28d12`, cursors 63,823–73,741, with **4,949 native/derived
delta pairs still retained**. Their payloads occupy **1,984,780 stored bytes**, excluding row/index
overhead. The exact delta templates, source references, item identities and completed summary all
match. One `HarnessStderr` at cursor 67,250 vetoes settlement for the whole span: Codex reported
that its approval policy refused an escalated command. Keeping that diagnostic is independent of
keeping thousands of duplicate reasoning chunks.

Running the settlement decision over the captured span rejects it at the anomaly check. Omitting
only the stderr entry from that decision produces a settlement covering all **9,898 delta rows**,
while preserving the start, completion and unrelated native notifications. This is a demonstrated
cause in one span, not attribution of all retained reasoning bytes to stderr.

The regression tests reproduce the veto with a smaller Codex span. Both the pure decision and
PostgreSQL append/read test fail before the change
([red invocation](https://app.buildbuddy.io/invocation/e0b9b324-75a4-4427-a6c7-9d8538949c9e)).
The changed policy retains stderr at its original cursor while allowing exact, otherwise eligible
delta pairs to settle. Failures, recovery boundaries, malformed frames and source-reference checks
continue to keep ineligible spans whole. Already stored items require a separate settlement pass;
changing the append path does not revisit them.

Another sampled Codex command retains deltas because its completed `aggregatedOutput` includes an
earlier prefix absent from the streamed deltas. The exact-concatenation rule rejects that case;
the stderr change does not broaden content matching.

## Historical settlement backfill

The [maintenance command](../README.md#historical-settlement) from
[PR #15908](https://github.com/agentydragon/ducktape/pull/15908) is published in
`devel-20261112234321-022c3c9`, digest
`sha256:63965691213dca97c0d76869cec6878229985f759453c6ff806066e8d3025fb2`.
Both History replicas were ready on that image before the apply.

The scoped dry run and apply agreed on **39 items, 216,902 delta rows and 43,714,014 stored
payload bytes** in Session `58e5d7cc-8e45-46e8-bf7b-8e8a79b28d12`. The apply Job
`history-settlement-apply-7g2tn` completed at 23:51:54 UTC on 2026-11-12.
The measured reasoning span now has 21 retained entries instead of 9,919. Its native/semantic
start and completion and stderr hashes are unchanged, as is the projected item's state hash.
There are no dangling delta evidence/native links for that settlement. Live `ReadPage`,
`ReadSequences` and late-removal reads recognize the settled gaps and retain cursor 3,884,665 as
the Session watermark; the measured span has 17 removal ranges. PostgreSQL and both History
replicas remained healthy.

The first dry-run Job landed on `wyrm2` (`atlas` zone), while PostgreSQL runs in `hil-ovh`.
It had not reached its first candidate after several minutes; its primary connection was waiting
on `ClientRead`. Recreating it in `hil-ovh` reached candidates after about 40 seconds and finished
in 84 seconds. The staging template pins that zone because the bounded scan makes many small
queries. No database content changed during either dry run.

The full read-only preview covered 163 Sessions and identified 115 eligible items across 27
Sessions: **243,088 rows and 49,604,130 stored payload bytes**. These payload counts exclude
row/index overhead and do not imply shrinking PostgreSQL relation files.

The first implementation took a projection write lease before checking whether a completion was
eligible, including already settled items. A regression with an ineligible item and a live projector
failed with `ErrProjectionBusy`. Checking eligibility read-only first avoids those unnecessary writes
and lease conflicts; eligible items are still rechecked under both write fences before deletion.

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

## After compaction: remaining amplification

Read-only replica measurements at approximately 01:03–01:19 UTC on 2026-11-13, after
operator-approved `VACUUM FULL`, find 4,173,159 retained events. `session_event` occupies
4,208.8 MiB including indexes and TOAST; its compressed payload values sum to 3,579,683,813 bytes.
The largest payload groups are boundary evidence (1,750.8 MiB), native harness frames
(1,196.9 MiB), semantic completions (214.2 MiB), and text deltas (82.6 MiB).

The sampling/dispatch prefix cohort contains 3,159 fragments and 1,535,756,111 stored payload
bytes (1.43 GiB). The previously measured largest sampler Session still has exactly 1,586 such
fragments and 984,351,175 stored bytes. All 115 saved sample-event payload hashes match live
History. Reassembling the six sampled requests proves that each contains equal structured
`body` and parsed `wire_request` values. Their contexts grow through 2, 215, 418, 635, 822 and
1,026 items; each later sample retains the entire previous sample as an equal prefix.

### Large completed items bypass settlement

The [settlement reader](../storage/store.go) returns no settlement when its backward scan would
exceed `settlementSpanLimit = 20_000`. This also governs the historical backfill. The cap counts
both native and semantic events, rather than text bytes.

In Session `91e0ad22-4ae2-45b0-a499-f617d54eb9ca`, reasoning item `rs_78038`, cursors
1,343,675–1,381,152, contains 18,737 native/text-delta pairs, one native/semantic start pair and
one native/semantic completion pair: **37,478 rows and 7,524,907 stored payload bytes**.
Its 71,336-byte semantic completion exactly matches the concatenated text deltas by SHA-256.
The native and semantic completion payloads occupy only 32,472 and 32,036 bytes respectively.
The span exceeds the reader's cap before the complete settlement proof can run. This is over
100 times the text size in stored event payloads, before row/index overhead.

A generated-protobuf scan of that Session and `58e5d7cc-8e45-46e8-bf7b-8e8a79b28d12`
finds 734,385 retained semantic text-delta rows carrying 2,894,970 text bytes, while their
payloads occupy 65,377,879 bytes. Fifty-four items have at least 10,000 text deltas each,
totaling 679,698 deltas before their native counterparts. Fifty-three items across the scan
have exactly one completion whose text equals all their retained deltas, accounting for
666,910 deltas. Text equality alone does not establish every other settlement precondition.
The fix should validate large spans incrementally while retaining the existing identity,
native-frame, anomaly and external-source-reference checks.

### Completed bodies and projection overhead

A fresh generated-protobuf/content scan of 3,263,454 event/projection rows confirms the same
1,629,022-byte tool output in native completion, semantic completion and projected output.
The previously measured 455,262-byte output still appears in 20 rows across five Sessions:
seven native completions, seven semantic completions and six projected chunks. Identical
results from distinct calls remain distinct observations; their bodies could share storage.

Projection chunk storage is 454.1 MiB, with approximately 282.7 MiB of stored JSON-text values;
output text accounts for 190.1 MiB of those values. Manifests occupy another 171.8 MiB.
The append path still shares prefix chunks, so summing manifest `content_bytes` overstates
actual retained body bytes.

`thread_evidence` and `thread_native_link` occupy 200.1 and 220.9 MiB. All 1,431,194 native-link
rows join an evidence row on their four shared key columns; evidence has 1,452,984 rows total.
Their [writer](../projection/recording.go) inserts both representations for observations with
native sources. These are metadata/reference costs, separate from repeated body content.
