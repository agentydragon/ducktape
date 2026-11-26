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
both cohorts against independent gzip**. No cross-session lookup was needed. Favor an
exact session-local representation for this subtype. These are comparisons for qualifying
sends, not for all History data. Selection favors sessions with large ordinary boundaries;
index space, row headers, lookup latency, WAL, disk allocation and delete cost were not measured.
A writable isolated database trial must include all of them.
