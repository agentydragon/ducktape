# Storing and repacking Sampler and History events in PostgreSQL

**Decision status: proposed; no writer-default change yet.** Keep each service's journal in
its own PostgreSQL database. Use PostgreSQL rows and B-tree indexes as the baseline. Where
cross-event compression pays, group original frames without interpreting their contents. The
first candidate for History boundary evidence is **same session + kind, 2 MiB or 32 entries,
Zstandard level 3**: it saved 72–73% against the current stored datums on two large-boundary
samples without extracting a single field. Keep a smaller read unit for other History streams.
Prefer a database-managed layout if it meets the same read, space and operations requirements.
Any winning policy must apply to existing data as well as new writes.

## Capacity comes before the rewrite

At 16:28 PST on November 25, kubelet reported **18.65 GiB available on the primary** and
**18.82 GiB on the standby**, each on a 48 GiB claim. A six-hour linear fit to available
bytes showed consumption of 9.39 and 8.98 GiB/day respectively; a 24-hour fit showed available
space **increasing** on both. At the short-window rate the primary would reach a 20% reserve
in about **23 hours** and fill in about **48 hours**. This is a burst scenario, not a forecast.
Recompute from kubelet_volume_stats_available_bytes per claim (use deriv over 6h and 24h);
do not add the two replicas' capacity or use the object-store backup slope for PVC growth.

1. Track the smaller of the two reserves and the time to exhaust it. As an operating trigger,
   if the six-hour slope predicts crossing 20% within 24 hours, verify the slope and available
   underlying storage now; prepare a declarative CNPG volume expansion. If growth continues,
   add headroom before starting a rewrite. Do not count projected compression as free space.
2. Canary the **new writer on incoming events** first. Measure stored bytes and WAL per million
   new events, append CPU/latency, replica lag and available bytes. This slows growth without
   duplicating the historical corpus. Preserve the ordinary fallback for a frame over the hard
   decoder cap. Roll out compatible readers before writing a new codec or block format.
3. Repack a small cold range only after both volumes can accommodate replacement blocks, WAL
   and the rollback/operating reserve. Stop if the budget shrinks. Normal vacuum enables page
   reuse; it does not promise filesystem space back. Do not run a full table rewrite to make
   room for itself. If there is insufficient runway, capacity expansion buys time independently
   of the long-term choice between native rows, database-managed compression and service blocks.

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

| Observation                                                                                                                                                                                                                                                              | Design consequence                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sampler stored 11.12M frames in 347,775 blocks: about 32 frames/block, already at the count limit on average. Gzip level 6 on the same framed bytes saved 13.2% and 14.2% in two samples against best speed.                                                             | Moving only the 2 MiB byte setting may change almost nothing. Vary the count cap independently. Level 6 is a credible cold-repack candidate; these samples did not measure foreground append CPU.                                                 |
| In History, 23,639 of 29,018 ordinary boundary entries exceeded the old 128 KiB limit. They accounted for 3.37 GiB, 96% of ordinary boundary payload bytes. Independently gzipping sampled ordinary boundaries used 39% fewer bytes than their stored PostgreSQL datums. | Fix singleton eligibility before tuning multi-entry blocks. This comparison includes the actual datum storage benefit PostgreSQL was already providing; it does not measure a switch to LZ4.                                                      |
| Of 66 and 48 sampled JavaScript sends, only 6 and 4 canonical tool catalogs were distinct. Sharing catalogs plus gzipping the remainder saved 90–91% against per-send gzip in that model.                                                                                | Use field reuse as a benchmark control. Canonical JSON equality across a sample does not prove byte equality within one session. The estimated 2.41–2.45 GiB opportunity is not a measured table reduction and cannot be added to the 39% saving. |

A [follow-up standby experiment](../debug/2026_11_25_postgres_event_compaction.md) verified exact
reconstruction on 94/94 and 110/110 JavaScript sends in two sets of 12 session windows. A
session-local catalog plus the remaining frame used **92.3–92.4% fewer encoded bytes than the
existing PostgreSQL datums**. Use this as an upper-bound comparison for sharing the field. Generic zstd
blocks already captured nearly all of that modeled saving on the same sends at an 8 MiB target. In a separate sample, 140 ordinary History datums
already compressed by PGLZ occupied 6.83 MB for 24.76 MB logical. PostgreSQL is a meaningful
control for the rest of the stream.

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

**History.** Make all 29 concrete journal event kinds eligible, grouped by session and kind.
The [existing packer](../history_service/internal/storage/blocks.go) supports only six.
Eligibility does not require creating a block: leave an underfilled tail readable and keep
ordinary rows when block metadata and encoding would cost more. Mutable state and projections
are separate from the immutable journal. For a same-kind stream keep actual cursors and original sizes, not an implied
dense interval. Merge cursor references before loading blocks for a page. Bound the number of
active streams; leave low-volume tails in ordinary indexed rows until another append or a cold
pass. Compare against mixed session-order blocks and against splitting only boundary records into
JavaScript sends versus other evidence. A split below kind requires a stream discriminator in the
index and a predecessor seek per stream: two substreams can have overlapping spans of the same kind.
Charge those seeks to the split. Do not make each tool name or each call ID a grouping key.

**Use self-contained blocks.** Use stock codec settings with no trained dictionary and no
application-managed catalog references. Every block must decode after restore using its bytes
and its format version alone. A codec can exploit repetition inside a block without a dictionary
artifact to train, distribute, retain or garbage-collect. The field-sharing experiment is a
comparison point: on the sampled sends, generic 8 MiB zstd blocks came within 1.4 percentage
points of its modeled storage saving. Reconsider a custom dictionary only through a separate
proposal showing a material workload benefit after pricing that lifecycle.

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

The [ordered-window sweep](../debug/2026_11_25_postgres_event_compaction.md#grouping-and-sealing-on-ordered-journal-windows)
gives concrete starting profiles: **64 KiB / 128 entries, session order, for Sampler** and
**64 KiB / 32 entries, per kind, for ordinary History streams**. The Sampler profile cut block count from 128
to 56 compared with the 2 MiB / 32 control while keeping mean point decode within 2 KiB.
The History profile trades a 4.0% encoded size premium over the smallest same-kind arm for
about one ninth of its mean point decode. Test 64 KiB / 256 if the extra index rows or scans
dominate real History read costs. Treat these as recommendations for the physical storage trial;
a block-size floor was not established. For History boundary records, use the separate
[large-event experiment](../debug/2026_11_25_postgres_event_compaction.md#can-generic-blocks-capture-the-large-records-without-parsing-the-catalog):
2 MiB / 32 zstd-3 admits large singletons and brings repeated bytes within codec reach. Test 8 MiB
for scans, but its roughly 7 MiB average point decode is a material cost.

Carry these controls and challengers into the remaining trials:

| Service | Control to retain for now                                                  | Candidate targets (KiB decoded) | Count and codec comparisons                                                        |
| ------- | -------------------------------------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------- |
| Sampler | Current 2 MiB hard bound, 32 frames, best-speed gzip                       | 64, 128, 256, 512, 1024, 2048   | 32 vs 128; current layout at level 6 as the measured size control; zstd 1 and 3    |
| History | Current 128 KiB hard bound, 256 entries, default gzip; keep existing reads | 64, 128, 256, 512, 1024         | 32 vs 256; include crossing singleton, on the original frames; same codec controls |

Do not derive H from the winning target. Set H from the reader's peak memory budget divided by
concurrent decoders, allowing for compressed input, frame indexes and output objects. Test that
limit with hostile lengths, truncation and high expansion before enabling a new version.

## One Go implementation, separate service profiles

Use a shared Go package for the new block format. Give its builder one ordered stream
of opaque wire frames and a typed writer profile (target framed bytes, target entry count,
codec and level). It handles framing, append-then-seal, tail flush and the too-large-for-format
outcome. The decoder reads the stored version and codec, enforces that version's hard limits
before allocating, and returns the original frames in order. Changing writer settings must
never change how an existing block decodes. Both live sealing and online repacking call the
same builder; given the same stream, profile and flush boundaries they produce the same groups.

| Caller                           | Stream supplied by service                         | Initial profile (target bytes / entries / codec) |
| -------------------------------- | -------------------------------------------------- | ------------------------------------------------ |
| Sampler                          | Contiguous sequence in one session; flush at a gap | 64 KiB / 128 / zstd-3                            |
| History: ordinary packable kinds | Ascending cursor in one session and kind           | 64 KiB / 32 / zstd-3                             |
| History: boundary evidence       | Same session and boundary kind                     | 2 MiB / 32 / zstd-3                              |

Select the profile at first packing and use the same rule on explicit repack. Neither service
promotes events to a different profile by age. The larger History target is a candidate for
boundary records because the sampled stream contains large repeated bytes. The sample was
selected by size and kind; it does not establish how reads vary with age. For Sampler the
ordered-window screen used gzip, and did not isolate similarly large boundary-heavy sessions
under zstd. If a matched zstd trial finds a benefit there, reevaluate the Sampler profile too.
An age tier would additionally need read traces by age and enough incremental savings to pay
for a second write, its WAL and the larger point reads.

Keep the profile values in each service's existing typed settings. Sampler owns the dense
sequence-range index. History owns kind selection, sparse cursor and size indexes, and the
merge for filtered pages. Each service also owns its legacy reader, SQL transactions, locks,
and durable repack checkpoint. The shared package needs neither a database handle nor an
event-kind enum. A common byte-exact round-trip suite covers both callers; service integration
tests cover seeks and page boundaries during a concurrent repack.

### When to cache decoded blocks

Start with one fetch and decode per distinct block within a read page, and release the buffer
when that page finishes. History already groups requested cursors by block before loading their
entries; Sampler walks blocks once per ordered range. PostgreSQL and the OS cache the stored
pages, but on a second request the service still transfers and decompresses the payload.

History point reads are the first candidate for a cache shared by requests on the same service
replica: the large-boundary sample would decode about 2.2 MiB per point lookup. That measures
the cost of a miss, not how often the same replica sees the same block again. Measure repeat
access on each replica and decode CPU by read shape; replay the trace against a byte budget
that fits the pod alongside concurrent decoders. Enable a cache only if a canary improves
read latency or CPU enough to justify its resident memory. Keep scans, exports and repacks
from populating it; one-pass traffic can evict the blocks point readers actually revisit.

If enabled, cache immutable decoded frame bytes and offsets, charging decoded size. Resolve
the authorized cursor through the current database snapshot first, then look up the resulting
immutable block identity. A session and first cursor alone are insufficient: a repack can put
different bytes at the same location. A replacement must get a new identity and a deleted
block must be unreachable from new snapshots. Test both cases with a warm cache. Keep this
policy at the service reader, outside the shared codec.

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

On a writable isolated PostgreSQL instance matching staging, create fresh identical tables per
arm. Set column storage and COMPRESSION before inserting plain framed values through the service
driver; copying existing toasted datums or merely changing the column default does not establish
that PostgreSQL re-encoded them. Check pg_column_compression and pg_column_size on the stored
column and compare pg_total_relation_size after identical loads, including auxiliary tables and
indexes. Measure WAL on an otherwise idle instance. pg_column_size alone excludes row and index
overhead; an in-process LZ4 run is not a measurement of PostgreSQL LZ4.

The [physical PostgreSQL trial](../debug/2026_11_25_postgres_event_compaction.md#physical-postgresql-layouts)
found that stock zstd blocks occupied **4.26 MiB** on the large-boundary sample, including
indexes and TOAST, versus **16.03 MiB** with one LZ4 row per event and **15.92 MiB** with LZ4
on the same grouped frames. Stock zstd blocks were also smaller on the two ordered-window
samples. For these high-volume streams, keep generic blocks as the candidate to beat on space;
prefer ordinary rows for streams where they meet the budget. Measure query and append latency
before changing defaults.

**What can replace the packer?** First test ordinary rows with a primary key on (session, cursor)
and, for History, an index on (session, kind, cursor). Use bytea with EXTENDED storage and compare
PGLZ and LZ4. The planner can satisfy page, filter and point queries directly. This removes our
block locators, decoder, sparse cursor merge and split/rewrite machinery; the engine handles
vacuum, recovery and failover. Prefer it wherever the measured physical size and growth fit the
capacity budget. Core PostgreSQL does not compress values across different rows. Declarative
partitioning helps prune partitions and drop an expired partition; it does not change this
compression boundary. pg_repack reclaims fragmentation by rewriting relations and needs scratch
space; it is not a journal codec or a first response to a nearly full volume.

For **engine-managed cross-row compression**, test a TimescaleDB hypertable with a row-oriented
hot tail and its columnstore on closed chunks, using session as a segment key and cursor as the
ordering key (also test session + kind for History). This could replace application grouping,
codecs and cold repacking with extension-managed chunks and let reads use ordinary SQL. Validate
that point reads prune chunks, sparse sessions fill useful segments, replay preserves cursor order,
range deletions work, and the partitioning column permits the required uniqueness constraint.
A time partition key cannot silently weaken uniqueness of (session, cursor). Measure the need
for a global locator; it could erase the simplicity advantage. A bytea containing an opaque
protobuf does not automatically get cross-event deduplication from a columnstore.

There is a [declarative CNPG TimescaleDB deployment](../../cluster/cdk8s/cdviz/timescale.py)
in this repository: pinned PG18 operand and extension images, preload, and a
[Database extension declaration](../../cluster/cdk8s/cdviz/database.py). It uses the OSS build;
do not infer that a columnstore API from a differently licensed distribution ships in it. Prove
feature availability, licensing, PG/image compatibility, upgrades, standby replay and restore
on a disposable cluster before proposing it for the shared Agentplane database. Do not put
an extension rollout on the critical path to slowing growth. An SQL compression function by
itself still needs someone to select, index and replace groups.

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
exist, keep the current writer defaults; use the measured generic packing results to prioritize
the experiment, not to authorize a fleet rewrite.

## Implementation order

1. Land the shared Go packer and versioned decoder with exact-byte and boundary tests.
   Test equality at the target, a crossing entry, a large singleton, and a frame exceeding the
   hard format limit. Keep existing formats readable.
2. Ship reader support before changing either writer. In History, fetch candidate block metadata
   for a page in one set-based query across the relevant kinds, including blocks straddling the
   requested cursor. Check query plans and latency with sparse and interleaved kinds; collapsing
   round trips alone does not guarantee less index work. Test exact cursor, kind filters, reverse
   pages and sequence reads with the full enum. In Sampler, allow the new spans and singletons.
3. Canary History writes using one packing switch, a default profile for every kind and a
   byte-target override for boundary records. Validate byte-exact replay, append and point-read
   p95, bytes per event and WAL at matched load. Then enable Sampler with its contiguous profile.
4. Dry-run the explicit repacker over ordinary rows and existing blocks, apply one bounded range,
   verify it against its original events, and verify a second run makes no changes. Expand in
   resumable, rate-limited batches while both PVCs have headroom and replica lag remains within budget.

## Repack already stored data

Make repacking an explicit operation over a frozen profile fingerprint and per-session high-water
marks. Enumerate **ordinary rows and existing blocks** in cursor order. Decode, reconstruct and
repartition; changing the codec on existing block boundaries alone cannot change grouping or size.
For sparse History streams expand each work unit to include whole overlapping source blocks in
the streams being replaced; if the closure exceeds the work budget, stage a replacement generation
in bounded chunks and swap its manifest atomically. Never move a checkpoint past an unprocessed
intersecting block.

Dry-run counts source bytes, target bytes, rewritten and skipped events and peak
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
