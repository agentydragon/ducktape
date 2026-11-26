# Storing and repacking Sampler and History events in PostgreSQL

**Decision status: proposed; no writer-default change yet.** Keep each service's journal in
its own PostgreSQL database. Start with a different grouping key for each service. In Sampler,
pack adjacent sequence numbers from one session. In History, pack eligible entries by session
and event kind; give repeated tool catalogs an exact-byte representation independent of the
block size. Compare both with PostgreSQL-managed compression on identical windows before choosing
the writer profile. A change of profile must be usable on data already stored.

## What we are optimizing

| Service | Writes and reads the layout must serve                                                                                                                                            | Implication                                                                                                                                                                                        |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sampler | Append under a session lock; recover a continuation, export an ordered range, follow the tail, resume inside a range                                                              | A contiguous sequence span is both a compact index and the unit of sequential I/O. Splitting by kind makes a single replay merge several streams.                                                  |
| History | Append while readers follow; seek by cursor, page forward/backward with kind filters and byte limits, fetch one piece of evidence, replay an entire session, remove cursor ranges | Keep a small uncompressed cursor/size index. A kind filter should be able to skip an entire block without fetching its TOAST value. Avoid inflating unrelated large evidence to read a small item. |

The unit of correctness is the original event at its cursor. Logical counts, byte limits, unknown
wire fields, authorization, retention, deletion and projections must be independent of packing.
Do not combine sessions: deletion and authorization are session-scoped. Compression ratio alone
cannot price a slower tail reader, an extra index lookup per event, or a longer append transaction.

## What the staging measurements establish

The [November 25 storage census](../debug/2026_11_25_staging_storage.md#which-bytes-are-worth-changing)
compared encodings on the same sampled values. Sizes are per PostgreSQL instance.

| Observation                                                                                                                                                                                                                                                              | Design consequence                                                                                                                                                                                                                             |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sampler stored 11.12M frames in 347,775 blocks: about 32 frames/block, already at the count limit on average. Gzip level 6 on the same framed bytes saved 13.2% and 14.2% in two samples against best speed.                                                             | Moving only the 2 MiB byte setting may change almost nothing. Vary the count cap independently. Level 6 is a credible cold-repack candidate; these samples did not measure foreground append CPU.                                              |
| In History, 23,639 of 29,018 ordinary boundary entries exceeded the old 128 KiB limit. They accounted for 3.37 GiB, 96% of ordinary boundary payload bytes. Independently gzipping sampled ordinary boundaries used 39% fewer bytes than their stored PostgreSQL datums. | Fix singleton eligibility before tuning multi-entry blocks. This comparison includes the actual datum storage benefit PostgreSQL was already providing; it does not measure a switch to LZ4.                                                   |
| Of 66 and 48 sampled JavaScript sends, only 6 and 4 canonical tool catalogs were distinct. Sharing catalogs plus gzipping the remainder saved 90–91% against per-send gzip in that model.                                                                                | Test exact serialized catalog reuse. Canonical JSON equality across a sample does not prove byte equality within one session. The estimated 2.41–2.45 GiB opportunity is not a measured table reduction and cannot be added to the 39% saving. |

### Choose what goes together

**Sampler.** Start one stream per session in sequence order. Close a block at a sequence gap;
never infer the contents of a missing cursor. Keep an indexed tail of ordinary events so append
can commit before a block fills. Prefer a background sealer on immutable prefixes if compressing
inside append delays the session lock. Enumerating kinds saves little if recovery immediately
needs every kind. Test a split by kind as a challenger, with its required cursor locator and
merge cost included. The [current reader](../sampler/internal/sessionstore/event_journal.go)
uses the 32-entry bound for its backwards seek and interprets a one-entry span as an ordinary
row. A larger count cap or compressed singleton needs explicit storage tags and a predecessor
lookup (or a reader bound valid for **all** stored versions) before enabling the writer.

**History.** Start with the [existing kind streams](../history_service/internal/storage/blocks.go):
native, text delta, tool-arguments delta, tool-output delta, boundary, and execution progress.
Keep mutable state, projections, live control rows and unstudied rare kinds in their current
representations. For a same-kind stream keep actual cursors and original sizes, not an implied
dense interval. Merge cursor references before loading blocks for a page. Bound the number of
active streams; leave low-volume tails in ordinary indexed rows until another append or a cold
pass. Compare against mixed session-order blocks and against splitting only boundary records into
JavaScript sends versus other evidence. A split below kind requires a stream discriminator in the
index and a predecessor seek per stream: two substreams can have overlapping spans of the same kind.
Charge those seeks to the split. Do not make each tool name or each call ID a grouping key.

**Repeated fields.** A sequence of large tool catalogs may exceed the codec's backward reference
window even when every catalog is adjacent. For a recognized JavaScript send, find the exact
serialized catalog slice, keep its surrounding frame bytes and lengths, and look up an immutable
catalog by session and digest. Verify length and byte equality before reuse. Keep the full frame
on parse ambiguity, digest mismatch, or when a reference plus lookup would cost more. Try a
block-local dictionary first and a session-local table second. The latter survives block
boundaries and handles a singleton; include its index, join and cleanup costs. Store references
atomically, authorize through the event, and delete an unreferenced catalog under the same
transactional ownership as its events. Exact reconstruction must succeed even for escaped JSON or
unknown protobuf fields. Sample hashes of canonicalized JSON cannot be used as storage identity.

### Choose where to seal

For each ordered stream, let T be the **target framed decoded bytes**, N the soft count limit,
and H the versioned maximum decodable framed size. Include frame lengths and headers. Each
stored version also declares a hard entry-count limit. T and N are writer settings.

1. Before appending, if the next frame would exceed H with the current group, flush the group.
   If the frame alone exceeds H, preserve the ordinary representation and advance past it.
2. Append the frame. Seal if bytes **>= T** or count **>= N**. Thus a 160 KiB entry at T = 128 KiB
   becomes a valid compressed singleton if it starts an empty group and fits H. If it follows
   30 KiB in the same stream and both fit H, seal a 190 KiB group (ignoring framing in this example).
3. On cold completion, flush the tail; on hot ingress, leave an underfull tail readable without
   recompressing it on every append. Compare stored bytes including the block header/index against
   leaving the entries ordinary. Keep the cheaper admissible representation. Incompressible data
   must neither cause a retry loop nor fragment an otherwise packable suffix.

Seed the experiment as follows; these are controls and comparison points, **not measured optima**:

| Service | Control to retain for now                                                  | Candidate targets (KiB decoded) | Count and codec comparisons                                                                |
| ------- | -------------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------ |
| Sampler | Current 2 MiB hard bound, 32 frames, best-speed gzip                       | 128, 256, 512, 1024, 2048       | 32 vs 128; current layout at level 6 as the measured size control; zstd 1 and 3            |
| History | Current 128 KiB hard bound, 256 entries, default gzip; keep existing reads | 64, 128, 256, 512, 1024         | 32 vs 256; include crossing singleton, with and without catalog reuse; same codec controls |

Do not derive H from the winning target. Set H from the reader's peak memory budget divided by
concurrent decoders, allowing for compressed input, frame indexes and output objects. Test that
limit with hostile lengths, truncation and high expansion before enabling a new version.

## What to let PostgreSQL do

TOAST can compress a single large datum and store it out of line. It knows neither the session
stream nor where two different events contain the same tool catalog. Conversely, the application
can frame a group once and let TOAST compress **the group**, retaining the benefit of repetition
across entries. Benchmark these independently:

| PostgreSQL layout                                  | Why it could win                                                              | What to charge                                                                                       |
| -------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| One bytea per event, EXTENDED storage, PGLZ or LZ4 | Simple append/delete and point reads; no packer                               | Row and index per event; no cross-event compression                                                  |
| Uncompressed framed block, EXTENDED, PGLZ or LZ4   | Same grouping and seek metadata as an application block; no application codec | PostgreSQL CPU and heap/TOAST I/O; whole-block fetch and decompression for a point read              |
| Application-compressed bytea, EXTERNAL storage     | Gzip or zstd sees the exact same frames; avoid asking TOAST to recompress     | Application CPU, encoded length, PostgreSQL chunk/index overhead; bounded reader and codec lifecycle |
| Each of the above with lossless catalog reuse      | Removes repeated bytes regardless of codec                                    | Reference and dictionary rows/indexes, lookups, deletion and cold restore                            |

On a writable isolated PostgreSQL instance matching staging, create fresh identical tables per
arm. Set column storage and COMPRESSION before inserting plain framed values through the service
driver; copying existing toasted datums or merely changing the column default does not establish
that PostgreSQL re-encoded them. Check pg_column_compression and pg_column_size on the stored
column and compare pg_total_relation_size after identical loads, including auxiliary tables and
indexes. Measure WAL on an otherwise idle instance. pg_column_size alone excludes row and index
overhead; an in-process LZ4 run is not a measurement of PostgreSQL LZ4.

Start with built-in compression. Consider a PostgreSQL extension only for a measured deficit
(e.g. a better cold codec without a service-owned encoder). Check that the pinned PostgreSQL
image contains the matching extension binaries or that CNPG supports a pinned extension image
for this cluster version. The [CNPG Database helper](../../cluster/cdk8s/cnpg/cluster.py) already
accepts declarative extensions; the [index service's vector setup](../../cluster/cdk8s/agentplane_index/README.md)
is a packaging precedent, not evidence of another extension's availability. Require the same
image on replicas and in restore tests. A SQL compression function does not replace TOAST's
compression method just because the extension is installed.

## Selecting a production profile

Sample ordered session windows on multiple days, stratified by volume, lifecycle (tail vs sealed),
and evidence subtype. Reconstruct windows from both ordinary entries and existing blocks; random
individual rows cannot test adjacency. Keep a held-out day. Replay identical events, seeds and
query traces for every arm. Record corpus coverage including oversized entries, gaps and
underfilled tails. Publish aggregate results and reproduction steps; keep original payloads in
the authorized environment.

First screen grouping, target and count in memory at a fixed codec. Validate the finalists
in PostgreSQL with actual ingestion, point reads (including large sends), filtered and unfiltered
pages, forward/reverse seeks, sequential recovery/export, and range deletion while readers run.
Record blocks fetched, compressed bytes read and **decoded bytes per returned byte** per read
shape; report hot/cold p50/p95/p99, append lock time, compression/decompression CPU, memory,
physical bytes per million events and WAL per new/repacked event. Repeat trials, alternate arm
order and report variation by session/day; weight by observed read frequencies without hiding
regressions in rare recovery paths. Separate service CPU from database CPU.

Reject any layout that fails byte-exact replay or the services' latency, throughput and memory
budgets. Among survivors choose the smallest retained footprint; if sampling variation obscures
the difference, choose the simpler representation and the smaller read unit. Publish one row per
service with winner, runner-up, measured trade-off and affected read path. Until these comparisons
exist, keep the current writer defaults; use the measured catalog and gzip findings to prioritize
the experiment, not to authorize a fleet rewrite.

## Repack already stored data

Make repacking an explicit operation over a frozen profile fingerprint and per-session high-water
marks. Enumerate **ordinary rows and existing blocks** in cursor order. Decode, reconstruct and
repartition; changing the codec on existing block boundaries alone cannot change grouping or size.
For sparse History streams expand each work unit to include whole overlapping source blocks in
the streams being replaced; if the closure exceeds the work budget, stage a replacement generation
in bounded chunks and swap its manifest atomically. Never move a checkpoint past an unprocessed
intersecting block.

Dry-run counts source bytes, target bytes, reused catalogs, rewritten and skipped events and peak
scratch space, without modifying journal rows. Apply outside the append critical path: prepare
bounded candidates, acquire the session lock with a short timeout, recheck source identities and
the removal watermark, then atomically replace the source and advance the checkpoint. Readers
use one MVCC snapshot. Skip a busy session, resume after failure, finish the opening watermark
even if new events arrive, and report already-conforming ranges on rerun. An operator can throttle
by bytes per second and concurrent sessions, and pause on replica lag, WAL growth, low disk headroom
or append/read latency. Keep old readers compatible before switching writers; rollback of the
writer policy does not roll back stored formats.

Acceptance: dry-run vs actual totals; larger-to-smaller and smaller-to-larger repacks; mixed codec
and format versions; cursor seek at every boundary; concurrent append, deletion, reader and crash
between batches; rerun with no writes; restore and replay through the opening watermark. Compare
the full ordered stream before and after, not just counts. Verify space reuse separately from bytes
returned to the filesystem: a successful online rewrite can initially **increase** allocated
space and archive WAL.
