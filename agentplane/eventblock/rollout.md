# Verify new journal blocks after a writer rollout

Use one active session per service, before starting its historical repack. The repacker and the
live writer produce the same envelope, so a header alone cannot tell you which one wrote it.

1. Verify that all replicas can read both formats. Capture a committed cursor for the selected
   session from the service's primary database: `session_history.last_cursor` for History,
   `sampler_session.cursor` for Sampler. Record it before enabling the new writer.
2. Once all writers have rolled out, let the session continue appending. Connect to the same
   service database on a caught-up standby with read-only access. Set the psql variables
   `session_id` and `watermark` to the values from step 1. Use this transaction and **one** of
   the queries below. Each reads at most 32 candidate blocks in primary key order and returns
   only positions and four-byte headers.

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';

-- History
SELECT first_cursor, encode(substring(payload FROM 1 FOR 4), 'hex') AS header
FROM session_event_block
WHERE session_id = :'session_id'::uuid AND first_cursor > :'watermark'::bigint
ORDER BY first_cursor DESC LIMIT 32;

-- Sampler (in its own database)
SELECT first_sequence, encode(substring(payload FROM 1 FOR 4), 'hex') AS header
FROM sampler_event_block
WHERE session_id = :'session_id' AND first_sequence > :'watermark'::integer
ORDER BY first_sequence DESC LIMIT 32;

ROLLBACK;
```

A header of `41504201` is evidence that a block with the APB v1 prefix reached the standby
for events after the recorded cursor. An empty result is inconclusive: the session may not
have sealed a new block, some events are stored standalone, or replication may be behind.
A header check neither validates decoding nor estimates how much historical data remains.

Before expanding the repack, use the service-specific inventory and dry run. Apply to one
active session, compare ordered replay bytes through its opening cursor before and after,
and verify that new appends still read back. Check write latency, replication lag and free
space before increasing the scope. Keep both readers until both inventories show no legacy
or unreadable blocks, skipped ranges have been accounted for, and a supported backup has
been restored and read successfully.

### Inventory by format

History's `session_event_block` and Sampler's `sampler_event_block` record the
versioned `block_format` and `uncompressed_bytes` when a block is written.
The byte count is the size of the uncompressed codec input, including framing.
It comes from the block header (APB v1) or the single gzip member's trailer.
These fields describe the encoding; replay still verifies its contents.

Existing rows initially have NULL metadata. Run [the metadata backfill](backfill_metadata.sql)
on each primary with psql autocommit enabled. It updates at most 128 blocks per
transaction and skips locked rows. Rerun until there are no NULL formats.
The backfill neither re-encodes payloads nor requires an idle writer.

Run this in the History database, substituting `sampler_event_block` for Sampler:

```sql
SELECT block_format, count(*) AS blocks, sum(uncompressed_bytes) AS uncompressed_bytes
FROM session_event_block
GROUP BY block_format;
```

Only declare the old format gone when there are no NULL, `unknown`, or
`legacy_gzip` blocks across both stores and every writer emits the new format.
