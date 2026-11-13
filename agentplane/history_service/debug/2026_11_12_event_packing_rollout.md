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

Final backfill totals and post-maintenance measurements will be added after the apply completes.
