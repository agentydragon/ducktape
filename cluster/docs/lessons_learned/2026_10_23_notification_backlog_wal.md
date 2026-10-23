# Notification backlog purge exhausted WAL headroom

On 2026-10-23, a one-off purge of staging's raw GitHub receipt backlog filled its primary
PostgreSQL volume and triggered failover. The purge was authorized, but the maintenance loop
outpaced WAL archival. Normal retention was not the source of this burst.

## Failure and recovery

The retention migration applied at 09:22:35 UTC. It stamped historical receipts with its own
transaction timestamp because prior ingestion times were unavailable. The operator approved
purging that cohort immediately rather than retaining it for another seven days.

The cohort contained 1,003,502 receipts at or below position 1,003,628, all with
`received_at = '2026-10-23 09:22:35.316019+00'`. Deletion used the service's GitHub advisory lock,
1,000-row transactions and a 50 ms yield. Bounded transactions limited individual lock holds,
but did not bound total WAL production.

After 800,000 committed deletions, `postgres-1` ran out of space at about 09:30 UTC: its 12 GiB
volume held roughly 3.9 GiB of WAL. The Barman sidecar had 239 unarchived 16 MiB segments.
Uploads were succeeding, typically taking 1–3 seconds per segment; the purge had simply outrun
them. A checkpoint could not recycle WAL that had not yet been archived.

CNPG promoted `postgres-3`. The former primary drained its archive queue, but pg_rewind then
failed repeatedly with `No space left on device` while copying recovery WAL. The operator
approved expanding both Piraeus volumes to 16 GiB and enabling in-use expansion. After expansion,
rewind and archive recovery completed. At 09:41 UTC both instances were Ready and `postgres-1` was physically
streaming from `postgres-3` with replay caught up. An Electric replication connection
alone had not established that the physical standby was back.

The remaining 203,502 receipts were deleted with one-second yields and gates before each batch:
at most 16 pending archive segments and less than 1.5 GiB in `pg_wal`. A gate that failed to clear
within 30 seconds stopped the script. These were incident-specific budgets, not fleet defaults.
Archive backlog stayed empty and the physical standby remained caught up during completion.

The operator separately approved compaction. Targeted `VACUUM (FULL, ANALYZE)` on
`github_delivery` and `github_delivery_subject` succeeded with a 500 ms lock timeout and a
five-second statement timeout. Only about 9.4 MiB of live raw payload remained to copy.
At 09:47 UTC the notifications database was 81 MiB, down from approximately 5.1 GiB;
`github_delivery` was 13 MiB and its subject links 512 KiB. Both filesystems used 3.2 GiB with
about 12 GiB free. All 9,285 inbox entries still had their payloads; post-migration receipts
remained, and new deliveries continued arriving.

## Maintenance constraints

- Check archival throughput and queued WAL as well as replica lag and free disk. Replication can
  be caught up while archival still prevents WAL recycling. `max_wal_size` is not a disk cap.
- Pace bulk deletion against archival and disk headroom; a small transaction size alone is not
  sufficient. Stop on failed gates rather than allowing a maintenance loop to consume all space.
- Never remove WAL files manually. Let CNPG archive and recover, and provide working space for
  rewind if needed. Verify the actual physical replication client, not just a healthy logical client.
- Measure logical deletion separately from physical reclamation. Plain vacuum makes dead space
  reusable; [VACUUM FULL](https://www.postgresql.org/docs/18/sql-vacuum.html) rewrites a table under
  an exclusive lock and needs space for its replacement. Use it only with explicit approval,
  sufficient headroom and bounded lock/execution limits for a live service.

Useful primary-side counters, alongside filesystem usage on both database Pods:

```sql
SELECT count(*) FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready';
SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir();
SELECT application_name, client_addr, state, replay_lsn FROM pg_stat_replication;
```
