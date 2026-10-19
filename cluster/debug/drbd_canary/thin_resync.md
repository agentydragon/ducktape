# FILE_THIN initial resync stall

## Observation (2026-10-19 UTC)

A 4Gi FILE_THIN first resync stalled for over 100 seconds with the writer
stopped: source Primary/UpToDate, target Secondary/Inconsistent, out of sync
`4,065,024 KiB` (about 3.08% reported progress), `68,680 KiB` sent,
`pending=2048`, `unacked=849`, `lower-pending=0`.

Changing the test volume's `rs-discard-granularity` to `1,048,576` bytes reduced
out-of-sync to `3,933,960 KiB` (about 128 MiB); it then held for about 30
seconds. Pending rose from 854 to 1117. Loop-device counters were unchanged over
10 seconds while ordinary `/dev/sdb` I/O advanced. The source data socket had
about 98,708 bytes queued and a 1,024-byte advertised receive window; the target
reported `rwnd_limited=85.5%`, empty receive queue, and 48-byte send queue. No
disk errors or hung tasks were observed. These counters show backpressure but
do not establish its cause.

After normal resource disconnect/reconnect, the source remained Primary/UpToDate
and DRBD returned to Connected/SyncSource: `3,800,064 KiB` out of sync (about
9.4% synchronized), `pending=44`, 13-second average `10.17 MiB/s`. Sync resumed;
subsequent samples advanced through 31%, 43%, 67%, 91% and 98% while the
writer ran. Both copies eventually became UpToDate. After quiescing and
detaching the source, same-PVC cutover completed at `03:34:38 UTC`; the
destination CRC32C check passed and only one local copy remained.

## Source-derived mechanism

The pinned module is DRBD `9.3.3` (peeled source commit
`97da76040a6b31aaf9e12f1a167e77ca2b3cb43e`). It sizes thin-resync requests from
`rs-discard-granularity`; an all-zero source read becomes a deallocated-range
reply. The target merges adjacent replies and submits trims when the range
reaches at least 128 MiB alignment, or a non-trim boundary, end marker, or
disconnect closes it. Thus a 128 MiB zero run is 32,768 requests at 4 KiB but
128 at 1 MiB. Small trim requests and deferred range submission plausibly
contributed: the observed first reduction was about 128 MiB. TCP counters also
show backpressure; neither observation proves the root cause.

A normal teardown forces remaining merged discards to submit and waits for
backing I/O before freeing unanswered requests. Successful discards mark their
ranges in sync; unanswered ranges remain for bitmap resync. The observed
reconnect resumed work without changing source role or disk state. The
exact-size request and all-zero reply path is in
[`drbd_sender.c` for v9.3.3](https://github.com/LINBIT/drbd/blob/drbd-9.3.3/drbd/drbd_sender.c#L1320-L1404);
trim aggregation and disconnect draining are in
[`drbd_receiver.c` for v9.3.3](https://github.com/LINBIT/drbd/blob/drbd-9.3.3/drbd/drbd_receiver.c#L9613-L10211).

## Allocation after cutover

After the move, online expansion from 4Gi to 5Gi and a separate 64Mi
write/delete/trim probe, the destination image occupied `146,030,592` physical
bytes (about 139MiB). It remained thin. The old source had already been removed,
so this does not isolate the allocation change caused by synchronization alone.
The 50Gi sparse probe was never moved. FILE_THIN shares XFS; the 20Gi per-node
stop threshold is operator-enforced, not a quota.
