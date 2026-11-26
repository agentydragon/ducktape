# PostgreSQL event compression: standby measurements, November 25

These experiments distinguish compression of individual PostgreSQL datums from reuse of a
field across immutable event records. Both ran on the staging read replica on November 25
(Pacific time; November 26 UTC). They complement the [storage census](2026_11_25_staging_storage.md)
and inform the [journal design](../plans/event_compaction_policy.md).

## What PostgreSQL compressed

PostgreSQL SYSTEM page sample (1%, repeatable seed 25), up to 1,000 records per table.
Use pg_column_compression on the stored column, pg_column_size for stored datum bytes, and
octet_length for logical bytea bytes. A null compression name means no PostgreSQL compression;
it says nothing about compression before the application supplied the value. Small headers can
make the stored size slightly larger than the logical length.

| Owner and value           | PostgreSQL method |  Rows | Stored bytes | Logical bytes |
| ------------------------- | ----------------- | ----: | -----------: | ------------: |
| History ordinary payload  | pglz              |   140 |    6,833,665 |    24,761,471 |
| History ordinary payload  | none              |   860 |      245,964 |       243,006 |
| History application block | none              | 1,000 |   10,488,038 |    10,487,814 |
| Sampler application block | none              | 1,000 |   16,465,208 |    16,464,296 |
| Sampler ordinary entry    | none              |    16 |      191,248 |       191,188 |

The 140 compressed History values were 72% smaller than their logical lengths. This does
not say PostgreSQL can achieve that reduction a second time on the already-compressed
blocks, nor does it compare PGLZ with LZ4. It establishes a strong per-event control.

## Does a session repeat the exact bytes of its tool catalog?

For each seed (25, 26), SYSTEM-sample 0.5% of History ordinary rows, keep at most 200
kind-31 anchors with payload length 128 KiB to 2 MiB, then take up to 12 distinct sessions
in session-key order at their lowest sampled anchor cursor. From each anchor read the next 20
ordinary kind-31 rows in the same session and exclude values above 2 MiB. Inspect only
JavaScript sends. These are windows in the **ordinary boundary stream**, not 20 adjacent
journal cursors. The two cohorts need not be disjoint.

Locate the unique serialized enabled_tools value at request.request.enabled_tools. Validate
the path against the parsed body. Retain the **original** UTF-8 slice, not sorted or reserialized
JSON. Require a unique occurrence in the full protobuf and verify that prefix, referenced value,
and suffix concatenate to the original frame. Scope the SHA-256 dictionary to one session
window, check byte equality on a hit, and reset between windows.

Compare the stored PostgreSQL column with gzip level 6 of each full protobuf and with this
lossless representation: gzip(prefix || suffix) plus a 32-byte reference and eight bytes
for the splice position per send; 32-byte key plus gzip(catalog) once per distinct value.
This prices a reconstructible encoding without pretending we wrote a new PostgreSQL table.

| Seed | Session windows | Verified sends | Distinct values summed over windows | Current stored bytes | Per-frame gzip bytes | Dictionary + references and remainders |
| ---: | --------------: | -------------: | ----------------------------------: | -------------------: | -------------------: | -------------------------------------: |
|   25 |              12 |          94/94 |                                  12 |           23,807,654 |           13,768,593 |         1,749,615 + 70,919 = 1,820,534 |
|   26 |              12 |        110/110 |                                  14 |           27,862,732 |           16,118,007 |         2,040,004 + 94,392 = 2,134,396 |

Session-scoped reuse saved **92.4% and 92.3% against the stored values** and **86.8% in
both cohorts against independent gzip**. No cross-session lookup was needed. This prices a specialized alternative to generic blocks. These are comparisons for qualifying
sends, not for all History data. Selection favors sessions with large ordinary boundaries;
index space, row headers, lookup latency, WAL, disk allocation and delete cost were not measured.
A writable isolated database trial must include all of them.

## Grouping and sealing on ordered journal windows

On the same standby, take a 0.2% SYSTEM sample (seed 81) of each service's block table, at
most 180 anchors. Select eight distinct sessions in key order, start at each session's earliest
sampled block, and read the next 512 cursor positions including intersecting blocks and ordinary
rows. Sort by cursor and check uniqueness. All eight windows per service passed the probe bounds
(32 or more events, no source value above 4 MiB, total event bytes at most 12 MB per window).
History contributed 3,615 events / 7,400,561 framed bytes; Sampler contributed 4,096 / 6,632,766.
Selection favors sessions already containing blocks. Gaps are possible in History.

For each window, pack all events either in session order or separately by event kind. Frame each
original entry with the same four-byte length for both arms. Append, then seal on reaching either
target; flush partial tails at the end. Compress each block with Python gzip level 1. Each aggregate
below includes tail blocks. “Point KiB” is the mean framed bytes decompressed to fetch a uniformly
selected event with no block cache: sum(block bytes × block event count) / total events / 1024.
This is a cost model from actual block membership, not measured point-read latency. CPU is one
process-time sample for framing and compression, excluding input decode and database work.

| Service | Group   | Target KiB / entries | Gzip bytes | Blocks | Point KiB | CPU ms |
| ------- | ------- | -------------------- | ---------: | -----: | --------: | -----: |
| History | session | 64 / 32              |  2,128,348 |    134 |      41.9 |   59.5 |
| History | session | 64 / 256             |  2,085,837 |     79 |      73.2 |   57.8 |
| History | session | 128 / 32             |  2,122,130 |    121 |      49.3 |   59.9 |
| History | session | 128 / 256            |  2,053,038 |     45 |     130.4 |   55.8 |
| History | session | 256 / 32             |  2,121,604 |    117 |      51.3 |   58.8 |
| History | session | 256 / 256            |  2,038,928 |     27 |     221.1 |   57.3 |
| History | session | 512 / 32             |  2,120,395 |    115 |      56.2 |   58.5 |
| History | session | 512 / 256            |  2,035,674 |     20 |     294.8 |   59.4 |
| History | session | 1024 / 32            |  2,120,787 |    115 |      56.4 |   60.0 |
| History | session | 1024 / 256           |  2,034,580 |     16 |     403.8 |   58.6 |
| History | session | 2048 / 32            |  2,118,628 |    114 |      63.9 |   59.3 |
| History | session | 2048 / 256           |  2,035,179 |     15 |     481.3 |   58.9 |
| History | kind    | 64 / 32              |  2,187,695 |    158 |      39.3 |   61.4 |
| History | kind    | 64 / 256             |  2,149,335 |    108 |      64.7 |   58.9 |
| History | kind    | 128 / 32             |  2,179,670 |    145 |      46.5 |   61.2 |
| History | kind    | 128 / 256            |  2,119,973 |     74 |     115.7 |   59.6 |
| History | kind    | 256 / 32             |  2,176,476 |    141 |      48.9 |   61.9 |
| History | kind    | 256 / 256            |  2,106,619 |     55 |     201.3 |   61.6 |
| History | kind    | 512 / 32             |  2,177,982 |    141 |      49.9 |   61.1 |
| History | kind    | 512 / 256            |  2,106,276 |     49 |     277.0 |   60.3 |
| History | kind    | 1024 / 32            |  2,177,377 |    140 |      50.6 |   62.9 |
| History | kind    | 1024 / 256           |  2,104,240 |     45 |     358.5 |   61.3 |
| History | kind    | 2048 / 32            |  2,177,974 |    139 |      59.4 |   61.6 |
| History | kind    | 2048 / 256           |  2,104,528 |     44 |     411.8 |   61.1 |
| Sampler | session | 64 / 32              |  1,563,977 |    141 |      22.1 |   49.7 |
| Sampler | session | 64 / 128             |  1,509,740 |     56 |      52.5 |   44.8 |
| Sampler | session | 128 / 32             |  1,565,450 |    136 |      26.0 |   51.4 |
| Sampler | session | 128 / 128            |  1,507,258 |     45 |      67.4 |   49.2 |
| Sampler | session | 256 / 32             |  1,565,740 |    135 |      31.3 |   48.7 |
| Sampler | session | 256 / 128            |  1,505,980 |     39 |     105.4 |   44.6 |
| Sampler | session | 512 / 32             |  1,566,965 |    133 |      34.1 |   48.0 |
| Sampler | session | 512 / 128            |  1,506,644 |     37 |     109.2 |   46.4 |
| Sampler | session | 1024 / 32            |  1,567,033 |    130 |      42.1 |   48.1 |
| Sampler | session | 1024 / 128           |  1,507,019 |     35 |     134.0 |   44.4 |
| Sampler | session | 2048 / 32            |  1,568,220 |    128 |      50.6 |   48.2 |
| Sampler | session | 2048 / 128           |  1,505,422 |     32 |     202.4 |   45.5 |
| Sampler | kind    | 64 / 32              |  1,524,568 |    174 |      16.6 |   47.1 |
| Sampler | kind    | 64 / 128             |  1,496,853 |     91 |      34.5 |   44.4 |
| Sampler | kind    | 128 / 32             |  1,524,531 |    165 |      21.3 |   48.6 |
| Sampler | kind    | 128 / 128            |  1,492,754 |     77 |      46.0 |   47.2 |
| Sampler | kind    | 256 / 32             |  1,526,391 |    162 |      31.8 |   50.8 |
| Sampler | kind    | 256 / 128            |  1,490,300 |     69 |      67.1 |   48.0 |
| Sampler | kind    | 512 / 32             |  1,527,444 |    160 |      34.4 |   50.5 |
| Sampler | kind    | 512 / 128            |  1,489,898 |     65 |     106.0 |   47.0 |
| Sampler | kind    | 1024 / 32            |  1,527,918 |    158 |      49.3 |   47.7 |
| Sampler | kind    | 1024 / 128           |  1,489,758 |     63 |     140.6 |   46.3 |
| Sampler | kind    | 2048 / 32            |  1,528,253 |    158 |      49.8 |   49.8 |
| Sampler | kind    | 2048 / 128           |  1,489,653 |     62 |     188.7 |   47.6 |

**Sampler:** start with session order, 64 KiB and 128 entries for the next PostgreSQL trial.
Compared with 2 MiB / 32 in this experiment it used 3.7% fewer compressed bytes and 56 vs 128
blocks, with similar mean point decode (52.5 vs 50.6 KiB). Raising only the target to 2 MiB
at 128 entries saved another 0.29% but increased point decode to 202.4 KiB. At 64 KiB / 128,
splitting by kind saved 0.85% relative to session order but produced 91 vs 56 blocks requiring
an additional locator/merge for ordered replay. Retain kind splitting as a challenger for
selective workloads, not the default for journal replay.

**History:** start with kind streams, 64 KiB and 32 entries for the read-sensitive path.
Storing 2,187,695 vs 2,104,240 bytes is a 4.0% premium over the smallest kind-stream arm,
but a point read decodes 39.3 vs 358.5 KiB. At the same 64 KiB target, raising the count
to 256 reduces block count from 158 to 108 and compressed bytes by 1.8% but increases point
decode to 64.7 KiB. Mixed session order at 64 KiB / 32 used 2.7% fewer encoded bytes; it
cannot exclude other kinds by block key. Measure kind-filtered pages as well as the physical
cost of those 158 blocks before choosing this over 64 KiB / 256. The tool-catalog representation
was not active in this sweep; it changes the input distribution and merits a fresh run.

The size and CPU columns isolate layout in one encoder. Neither reproduces the deployed Go
encoder or its existing eligibility rules. This probe packed all kinds and used four-byte framing
in both services; it did not price PostgreSQL tuples, indexes, seeks, or the cost of reading an
actual filtered page. No family of unmeasured read frequencies is implicitly assigned a weight.

## Sampler sequential read model

Both the History exporter and admin `FollowEvents` read forward from a cursor. For a backlog,
the journal store fetches and decodes each block intersecting each requested page. A block
straddling two pages is decoded twice, even though only the requested events are returned.
At the live tail a reader takes whatever is available, often from ordinary rows before they seal;
a configured page size is a ceiling, not a reason to wait.

Use the eight session-order windows above (4,096 events, 6,632,766 framed bytes). Let a sealed
block contain `b` consecutive events and `d` framed bytes. For full pages of `p` events and a
uniformly distributed starting phase relative to the blocks, each of its `b - 1` internal
event boundaries has probability `1/p` of splitting a page. The block is decoded on average
`1 + (b - 1)/p` times. Across a full scan the expected ratio of decoded bytes to journal bytes
is `1 + (sum(b*d)/sum(d) - 1)/p`. The numerator is the existing point-read experiment's
weighted byte total. For a known aligned cursor, count actual internal page boundaries instead:
a 128-event block on 16-event page boundaries costs eight decodes rather than the phase average of 8.94.
A reader that carries the last decoded block across pages consumes each block once, at any page size.

| Target KiB / entries | Gzip bytes | Blocks | 16 events/page |    32 |    64 |   128 |   256 |
| -------------------- | ---------: | -----: | -------------: | ----: | ----: | ----: | ----: |
| 64 / 32              |  1,563,977 |    141 |          1.81× | 1.41× | 1.20× | 1.10× | 1.05× |
| 64 / 128             |  1,509,740 |     56 |          3.01× | 2.01× | 1.50× | 1.25× | 1.13× |
| 128 / 32             |  1,565,450 |    136 |          1.97× | 1.48× | 1.24× | 1.12× | 1.06× |
| 128 / 128            |  1,507,258 |     45 |          3.60× | 2.30× | 1.65× | 1.33× | 1.16× |
| 256 / 32             |  1,565,740 |    135 |          2.18× | 1.59× | 1.29× | 1.15× | 1.07× |
| 256 / 128            |  1,505,980 |     39 |          5.10× | 3.05× | 2.03× | 1.51× | 1.26× |
| 512 / 32             |  1,566,965 |    133 |          2.28× | 1.64× | 1.32× | 1.16× | 1.08× |
| 512 / 128            |  1,506,644 |     37 |          5.25× | 3.13× | 2.06× | 1.53× | 1.27× |
| 1024 / 32            |  1,567,033 |    130 |          2.60× | 1.80× | 1.40× | 1.20× | 1.10× |
| 1024 / 128           |  1,507,019 |     35 |          6.23× | 3.62× | 2.31× | 1.65× | 1.33× |
| 2048 / 32            |  1,568,220 |    128 |          2.94× | 1.97× | 1.48× | 1.24× | 1.12× |
| 2048 / 128           |  1,505,422 |     32 |          8.94× | 4.97× | 2.98× | 1.99× | 1.50× |

For the existing 64 KiB / 128-entry target, try 64 events per export page and four pages per
worker turn: that keeps the 256-event scheduling quantum while cutting expected decode volume
from 3.01× to 1.50× and full-page acknowledgement cycles from 16 to four. At that page size,
reducing the block cap to 32 lowers decode volume further, to 1.20×, but uses 141 blocks
instead of 56 and 3.6% more compressed bytes. Raising the byte target to 2 MiB at 128 entries
uses only 0.29% fewer compressed bytes, while decode rises to 2.98×. Keep 64 KiB / 128 as the
initial block profile for this trial. With a block-aware reader the decode penalty disappears;
then the smaller number of database rows could favor a larger target.

These are modeled bytes for a sealed catch-up scan, not measurements of latency, PostgreSQL
I/O or peak heap. Compression sizes here use gzip-1; the deployed APB writer uses zstd-3.
The sample favors already-blocked sessions. Before changing rollout defaults, compare 16 × 16
and 64 × 4 on the same zstd backlog, measuring catch-up time, exporter peak memory, and the
delay before another pending session gets a turn. Include a live follower separately. No
production mix of catch-up, tailing, or resume offsets is assumed.

## Can generic blocks capture the large records without parsing the catalog?

Yes. Reuse the two 12-session History samples above. For each session, collect the same 20
ordinary boundary records from the anchor, up to 2 MiB per original value. Keep full wire
frames in cursor order. Append four-byte lengths, seal after crossing the target or 32 entries,
flush the tail, and enforce a separate 8 MiB framed limit before append. No digest, catalog
parser, delta template or shared field participates in encoding. Run Python gzip level 6 and
libzstd level 3 on the identical bytes. Repeat for just the JavaScript sends from these windows
to isolate the benefit of a finer grouping key. Identify that subtype from the event envelope;
do not transform its body. The two cohorts can overlap. Sizes below are encoded bytes only.

| Seed | Stream   | Events (>128 KiB) | Target KiB | Stored PostgreSQL |     Gzip 6 |     Zstd 3 | Blocks (singletons) | Mean point decode MiB |
| ---- | -------- | ----------------: | ---------: | ----------------: | ---------: | ---------: | ------------------: | --------------------: |
| 25   | boundary |         240 (140) |         64 |        29,770,131 | 18,145,353 | 18,479,108 |           240 (240) |                  0.38 |
| 25   | boundary |         240 (140) |        512 |        29,770,131 | 18,178,983 | 16,464,324 |            103 (37) |                  0.97 |
| 25   | boundary |         240 (140) |       2048 |        29,770,131 | 18,135,098 |  8,053,099 |              46 (0) |                  2.14 |
| 25   | boundary |         240 (140) |       8192 |        29,770,131 | 18,126,081 |  3,386,376 |              14 (0) |                  6.77 |
| 25   | js       |           94 (94) |         64 |        23,807,654 | 13,769,404 | 13,926,904 |             94 (94) |                  0.79 |
| 25   | js       |           94 (94) |        512 |        23,807,654 | 13,769,404 | 13,926,904 |             94 (94) |                  0.79 |
| 25   | js       |           94 (94) |       2048 |        23,807,654 | 13,717,208 |  5,065,500 |              34 (3) |                  2.28 |
| 25   | js       |           94 (94) |       8192 |        23,807,654 | 13,700,934 |  2,112,626 |              14 (1) |                  5.87 |
| 26   | boundary |         240 (183) |         64 |        34,168,266 | 20,653,807 | 21,024,815 |           240 (240) |                  0.45 |
| 26   | boundary |         240 (183) |        512 |        34,168,266 | 20,686,826 | 19,115,042 |            129 (68) |                  0.90 |
| 26   | boundary |         240 (183) |       2048 |        34,168,266 | 20,640,923 |  9,454,368 |              53 (3) |                  2.17 |
| 26   | boundary |         240 (183) |       8192 |        34,168,266 | 20,624,633 |  4,655,853 |              16 (0) |                  6.97 |
| 26   | js       |         110 (110) |         64 |        27,862,732 | 16,118,971 | 16,302,031 |           110 (110) |                  0.79 |
| 26   | js       |         110 (110) |        512 |        27,862,732 | 16,118,971 | 16,302,031 |           110 (110) |                  0.79 |
| 26   | js       |         110 (110) |       2048 |        27,862,732 | 16,059,173 |  6,148,496 |              41 (5) |                  2.25 |
| 26   | js       |         110 (110) |       8192 |        27,862,732 | 16,037,863 |  2,517,819 |              15 (0) |                  6.32 |

**Production candidate for History boundary evidence: same session and kind, 2 MiB soft
target, 32 entries, Zstandard level 3.** Against current stored datums it saved 72–73% on all
boundary records in these selected windows. An 8 MiB target increased that to 86–89%, but a
uniform point fetch would inflate about 7 MiB instead of 2.2 MiB. Reserve the larger target for
a cold-scan trial. With only JavaScript sends, 8 MiB generic zstd was within 1.2–1.4 percentage
points of the specialized catalog model as a fraction of current storage. That narrow incremental
opportunity does not justify maintaining a field dictionary without a separate read-path win.
Gzip's smaller match window explains why merely raising its block target did little. On the
full boundary samples at 2 MiB the zstd encoder consumed 0.14/0.17 CPU seconds vs 1.82/2.07 for
gzip; these are local library calls, not service append latency.

These windows deliberately target formerly excluded rows and do not estimate fleet-wide savings.
They justify a boundary-specific trial, not larger blocks for sparse ordinary History point reads.

## Physical PostgreSQL layouts

Read source data on the standby and load fresh bytea values into a disposable local PostgreSQL
instance on a separate scratch filesystem. Use six session windows per cohort: seed 25 and the
next 20 ordinary boundaries as above (120 events); seed 81 and up to 512 positions for ordered
History (2,655 events) and Sampler (3,072). Use the same events in every arm. Create a fresh
table with a synthetic integer session key, cursor, kind, optional cursor array, and payload.
Index (session, cursor) and, for History, also (session, kind, cursor). The modeled block layout
stores sparse cursor arrays for History; it does not price the production sizes array or all
ordinary-row metadata. This is a physical comparison of the candidate layouts, not a production
capacity forecast.

Compare one row per original event against four-byte length-framed groups. Use the same groups
for both PostgreSQL codecs (EXTENDED storage) and dictionary-free zstd level 3 (EXTERNAL).
Measure pg_total_relation_size after loading, including heap, indexes and TOAST. There were no
live database writes. These trials did not measure steady-state allocator reuse, WAL or latency.

| Window / grouping                                     |   Row PGLZ |    Row LZ4 | Block PGLZ |  Block LZ4 | Block zstd-3 |
| ----------------------------------------------------- | ---------: | ---------: | ---------: | ---------: | -----------: |
| History boundary, 2 MiB / 32 by kind (25 blocks)      | 17,760,256 | 16,809,984 | 17,702,912 | 16,695,296 |    4,464,640 |
| History ordered, 64 KiB / 32 by kind (113 blocks)     |  3,153,920 |  3,293,184 |  2,015,232 |  1,998,848 |    1,433,600 |
| Sampler ordered, 64 KiB / 128 in sequence (46 blocks) |  2,990,080 |  3,047,424 |  2,064,384 |  1,974,272 |    1,376,256 |

All figures are allocated bytes. On the boundary sample, grouping with LZ4 alone saved less
than 1% versus individual LZ4 rows. Stock zstd on the same groups cut the physical footprint by
73% versus individual LZ4 rows. It needed neither a trained dictionary nor knowledge of the
boundary subtype. For the other samples, plain framed groups compressed by PostgreSQL already
beat individual rows, though stock zstd groups were smallest on disk.
