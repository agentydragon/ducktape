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
