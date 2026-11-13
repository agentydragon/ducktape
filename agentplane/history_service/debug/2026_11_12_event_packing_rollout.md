# Staging event packing rollout

Operation began November 12, 2026 Pacific time; timestamps below are UTC November 13.
This is lossless packing of retained EventEntry protobufs, separate from semantic delta settlement.
Earlier amplification measurements are in [the storage investigation](2026_11_12_retained_request_amplification.md).

## Deployment and new writes

[PR #16009](https://github.com/agentydragon/ducktape/pull/16009) implemented packing behind
`ingress.compact_deltas`. [PR #16046](https://github.com/agentydragon/ducktape/pull/16046)
enabled it in staging and added the historical maintenance command. Its focused 17-target run
passed ([BuildBuddy](https://app.buildbuddy.io/invocation/6fb159c3-51fa-4be6-ac3a-62770156b250));
PR CI, including the Pod system test, passed before merge.

Both serving replicas reached migration 16 with packing enabled. The maintenance image was
`agentplane-history-service:devel-20261113035040-30bb39d`, a descendant of both PRs.

The [operator probe](../ops/probe_ingress.go) used actual authenticated ingress and public reads,
verified server identity using the workload CA and History SPIFFE URI, and invoked no harness or
provider. Short-lived Pod-bound credentials were removed after use. Its dedicated Session
`b6512482-80b5-4151-b6e8-59aaa41fc281` acknowledged and sealed 162 events. All 162 read back with
`proto.Equal`; an 11-event page after cursor 17 also matched. The journal ends before item completion,
so this checks incremental packing independently of settlement.

Before any historical apply, SQL found native block cursors 1–141 with 71 events / 926 encoded bytes
and a text-delta block at 4–140 with 69 events / 783 bytes: 140 events in two blocks plus 22 ordinary
rows. This proves live ingress packing, beyond readback alone. Actual
[probe invocation](https://app.buildbuddy.io/invocation/60059222-e64d-4010-a804-28852efb3bb4).

## Historical maintenance

At 03:13:56 the History database (`sandbox_service`) occupied 5,745,440,447 bytes;
`session_event` occupied 4,413,243,392 bytes including indexes/TOAST. There were no blocks.
There were 2,955,570 ordinary eligible-sized native/text/tool-argument/tool-output events.
These counts exclude oversized native entries and all non-packable event kinds.

The canary was Session `91e0ad22-4ae2-45b0-a499-f617d54eb9ca`. Its dry run found 992,654 events
in 3,948 blocks, converting 208,681,273 protobuf bytes into 26,729,503 encoded bytes. These are
payload measurements, not physical relation sizes.

The first apply was suspended at 03:58:50 when logical-slot retention approached 377 MB and the
archive queue grew. The slot limit is 512 MB, so a fixed 50 ms inter-block pause alone was insufficient
for this shared database. Electric remained healthy and caught up; no slot was invalidated.

The resumed Job used a 50m CPU limit and an external maintenance guard, suspending the Job above
128 MiB retained WAL, 16 pending archive segments, or 64 MiB physical replay lag. It resumes below
32 MiB / four segments / 16 MiB respectively. Suspension preserves committed blocks; restarts skip
them. No PostgreSQL settings, replication slots, History watermarks, or projection state were reset.
Each block transaction reads back and compares every moved event before commit.

The canary finished with exactly its dry-run totals. The all-session pass, using a 200m CPU limit
and the same guard, completed at 04:34:04. A subsequent dry run scanned all 164 Sessions and reported
zero further events, blocks or entries skipped inside blocks. There are 2,500 eligible-sized ordinary
entries left as small tails. Oversized frames and other event kinds remain ordinary.

Final storage contains 2,953,231 events in 18,938 blocks, including the probe's 140 events / two blocks.
Thus the historical conversion moved 2,953,091 events into 18,936 blocks. The blocks represent
1,841,875,264 protobuf bytes in 313,987,012 gzip bytes, excluding row/index/array metadata.
All moved events passed the per-transaction production-reader equality check. SQL independently
found zero overlapping per-kind block spans. The measured oversized reasoning span still has
37,478 events at distinct original cursors 1,343,675–1,381,152.

## Full vacuum and final checks

The operator explicitly requested `VACUUM FULL` after compaction. The original 512 MB replication-slot
limit was temporarily raised to 8 GB through the CNPG Cluster parameter; both instances reloaded it.
The parent Flux reconciliation overwrites a child Kustomization's temporary `suspend: true`, so the
maintenance hold was narrowed to the Postgres resource using
[`kustomize.toolkit.fluxcd.io/reconcile: disabled`](https://fluxcd.io/flux/components/kustomize/kustomizations/#suspending-and-resuming).
Other staging reconciliation continued. This leaves CNPG managing the database throughout.

`VACUUM (FULL, ANALYZE, VERBOSE) session_event,session_event_block` ran from 04:35:49 to 04:36:42,
finishing successfully in 52.95 seconds. The session used `wal_compression=on`, a five-second lock
timeout and a 30-minute statement timeout. A separate monitor would cancel this named maintenance
query above 6 GiB retained WAL, below 4 GiB free on either instance, or on a slot/retention failure.
Observed retained WAL peaked at 2,711,953,544 bytes, demonstrating why the 512 MB limit was unsuitable
for the rewrite. Neither slot was invalidated. Minimum observed free space was 11,762,204,672 bytes
on the primary and 12,763,275,264 on the replica.

| Allocated bytes, including indexes and TOAST | Before packing | After packing and full vacuum |
| -------------------------------------------- | -------------: | ----------------------------: |
| History database                             |  5,745,440,447 |                 4,316,198,591 |
| `session_event`                              |  4,413,243,392 |                 2,607,546,368 |
| `session_event_block`                        |         32,768 |                   376,471,552 |

The database reclaimed 1,429,241,856 bytes (24.9%). Packing initially increased allocated files;
the full vacuum returned the superseded ordinary-row space to the filesystem.

After the full vacuum, every original Session's watermark was unchanged. Logical event counts,
including packed entries, matched the baseline by kind plus exactly the probe's 162 events:
4,173,159 original events and 4,173,321 final events. No original Session disappeared and the probe
was the only new Session. The public ingress/readback probe also passed again after the rewrite
([BuildBuddy](https://app.buildbuddy.io/invocation/27f2540f-4846-4dea-8b99-be46b7938037)), including
the page beginning inside a block. Short-lived token files were deleted again. Both History replicas
were available and Electric returned HTTP 200 with `{"status":"active"}`; physical replication was
caught up and the logical slot was current. WAL archiving had no recorded failures. Once both slots
were current, the original 512 MB retention limit was restored and verified on both instances with
no pending restart. The Postgres maintenance annotation was removed; staging Flux was not suspended.
The superseded suspended canary Job was deleted, with its committed work retained and verified.
At 04:48:04, the final archive queue was empty, physical replay lag was zero, and both slots were
active and reserved. After a checkpoint, the primary had 16,901,935,104 bytes free and the replica
17,455,464,448 bytes free. Local port-forwards and probe credentials were removed.
