# OVH NVMe dropout and DRBD resync stall — 2026-11-20

**Status: open.** A data NVMe stopped responding, and reattaching its DRBD
volumes after a node reboot restored most services. A second reboot cleared a
stalled CLI Proxy resync. A damaged SeaweedFS replica was removed after its
files were preserved; the physical fault remains unexplained and one older
SeaweedFS record still needs a filer-reference check.

## Timeline

Times are UTC on 2026-11-20. Kernel timestamps refer to the event, not the
later time at which the log collector received it.

| Time              | Observation                                                                                                                                                                                    |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 04:44             | Thin-pool data usage was 39.22%, metadata usage 1.96%, and the affected NVMe's composite temperature 33.85 °C.                                                                                 |
| 04:45:56          | The kernel logged NVMe read and write timeouts. It attempted a controller reset at 04:46:26.                                                                                                   |
| 04:47:38–04:48:59 | The controller did not become ready; the kernel disabled the device. DRBD detached Authentik's local database disk after I/O errors, and ext4 aborted its journal.                             |
| 05:06             | Authentik's server was restarted after failing liveness checks. PostgreSQL had exhausted its ordinary connection slots.                                                                        |
| 05:14–05:17       | Talos began shutting down but could not unmount busy CSI volumes. A later forced reboot request failed to acquire the existing reboot lock.                                                    |
| 05:52–05:54       | The OVH hard-reboot task completed, and the NVMe was rediscovered during boot.                                                                                                                 |
| 06:11–06:22       | Reattached the affected existing volumes. Authentik's former primary ran `pg_rewind` and rejoined as a standby. At the last check all CNPG clusters reported their configured instances ready. |
| 07:32–07:43       | After normal DRBD disconnect was refused, drained the node except for stuck CLI Proxy pods. Graceful Talos reboot stalled; OVH hard reboot completed at 07:43.                                 |
| By 07:48          | The node returned, CLI Proxy's two diskful replicas resynced to `UpToDate`, and the application became Ready and served model-list requests.                                                   |
| 08:23             | Removed the quarantined SeaweedFS replica after preserving its files. Two writable copies remained, matching the configured replication count.                                                 |

## Incident and recovery

An OVH control-plane node's data NVMe timed out on reads and writes. The kernel
could not reset the controller and disabled the device. This was not thin-pool
exhaustion: the last pre-failure sample showed data usage below 40%. The
recorded drive temperature was around 34 °C just before the incident.

Volumes lost their backing storage, including an Authentik PostgreSQL primary.
Its blocked requests accumulated until the database exhausted available
connections, and Authentik became unavailable. A CloudNativePG health report
initially continued to show the database as healthy despite blocked I/O.

The requested Talos reboot got stuck during shutdown on busy volume mounts; a
second Talos reboot request could not acquire the existing reboot lock. An OVH
hard reboot brought the node and NVMe back. Authentik failed over to its
surviving database instance. The drive's post-reboot NVMe health log reported
no critical warning or media errors, which does not explain or disprove the
observed controller failure.

The post-reboot NVMe health log reported 55,959 powered-on hours, 13% endurance
used, 100% available spare and zero media errors or error-log entries. These
are device-reported lifetime counters; the pre-incident values were not
captured. They cannot rule out an intermittent controller, device, PCIe or
power fault. During the two hours before failure, the composite-temperature
samples ranged from 33.85 to 39.85 °C. Around 04:43, five-minute average
device throughput was approximately 51 MB/s written and 61 MB/s read; this
does not establish a causal workload or an instantaneous rate at failure.

At the operator's request, cleared LINSTOR's `DrbdOptions/SkipDisk` on the
affected resources after confirming their backing logical volumes existed.
Database instances mounted and rejoined their primaries. Three idle primaries
needed the documented [CNPG recovery nudge](../docs/cnpg_conventions.md#gotcha-replica-stuck-01-after-failover)
to advance WAL past the standby recovery point. All CNPG clusters subsequently
reported their configured instances ready; an Authentik standby's replay
position matched its primary. Casino's login route reached the Authentik flow;
an authenticated login was not tested.

One returned SeaweedFS copy of the collector spool reported an index pointing
beyond its data file and was marked read-only. Its index contained one live
entry absent from the two writable copies; it may be a deletion removed during
compaction. A two-way index check and full scrub found no problems in the two
writable copies. Their captured files had different whole-file hashes, but all
80 live indexed records had matching headers and bodies at the time of the
check. A three-minute filer-wide reference scan timed out, so whether the extra
record was referenced elsewhere remained unknown.

At the owner's request, the damaged extra replica was removed at 08:23 after
its four volume files had been preserved privately and checked against the
original hashes. The two writable copies remained, satisfying the configured
two-copy policy; no automatic replacement is needed. The collector stayed
Ready, and a 4 KiB read of an existing spool file through the filer succeeded.
That did not test a read through the collector's own FUSE mount. The preserved
files are local sensitive recovery material, not a verified durable backup.

## DRBD resync ordering wait

After its former diskless primary reattached its old local disk, a replicated
CLI Proxy state volume stayed `Inconsistent` at 0% resync. The source held
thousands of resync reads in `dagtag_wait`; the source and target remained
connected. The target's resource ordering counter was much higher than the
last tag the source had received from it. Small direct reads of the underlying
logical devices completed, so those probes did not reproduce a disk read stall.

At the investigation snapshot, the target's resource tag was `335004`, while
its last tag sent to the source and the source's last received tag were both
`2966`. DRBD reported 4,159 resync requests pending and the whole 266,144 KiB
out of sync. The source's oldest queued resync reads had waited over 23 minutes.
These are ordering and queue observations, not a measurement of disk capacity
or proof of the exact dependency in each queued request.

The installed DRBD 9.3.3 code gives the primary's current ordering tag to
resync requests and queues source-side reads until the relevant peer's tag has
been observed. [Sender](https://github.com/LINBIT/drbd/blob/97da76040a6b31aaf9e12f1a167e77ca2b3cb43e/drbd/drbd_sender.c#L396-L435),
[receiver](https://github.com/LINBIT/drbd/blob/97da76040a6b31aaf9e12f1a167e77ca2b3cb43e/drbd/drbd_receiver.c#L3715-L3799).
The reattached disk's metadata contained a bitmap tag matching the target's
higher counter. The [attach code](https://github.com/LINBIT/drbd/blob/97da76040a6b31aaf9e12f1a167e77ca2b3cb43e/drbd/drbd_nl.c#L2664-L2688)
can raise that counter from stored metadata.

**Hypothesis:** attaching the old disk advanced the running primary's resource
counter without its already-connected source receiving the corresponding
ordering update. Resync reads then waited for a tag the source had not seen.
This is consistent with the observed queue and code, but the live debug format
did not expose each queued request's dependency value, and no pre-attach
counter snapshot was captured. A raw superblock snapshot taken while attached
can also be stale. A specific DRBD defect or upstream fix is not yet proven.

To recover, we made a read-only thin snapshot of the `UpToDate` replica on the
other node and copied it off-node; the image checksum matched the snapshot.
The image was crash-consistent and its ext4 filesystem required journal
recovery. A normal peer disconnect was refused because the primary needed the
remote up-to-date data. We moved database primaries off the affected node and
evicted its other workloads; the CLI Proxy pods remained stuck terminating.
Graceful Talos shutdown also stalled on those containers, so an OVH hard reboot
was used. On return, the queued requests had cleared and the two diskful
copies resynced to `UpToDate` with zero out-of-sync data. CLI Proxy became
Ready, loaded three credential files and served `/v1/models` requests; an
actual model inference was not verified. One Claude credential reported an
invalid refresh token. The read-only snapshot and a private image in temporary
storage were retained as recovery copies.

### Tentative lesson

The evidence points to reattaching a disk to an already active diskless
primary as a possible trigger, not to the manner of the original node reboot.
We cleared `SkipDisk` on 44 resources in a batch. In a future incident, inventory
each resource and its authoritative copy, quiesce consumers of replicated
resources before reattachment, and proceed one resource at a time. Capture
DRBD state before and after attachment and verify the replica is `UpToDate`
with no out-of-sync data before returning its application to service. This is
a precaution: the missing pre-attach counters and request dependencies mean we
cannot say that this sequence would have prevented the stall.

Reproduce the diskless-primary reattachment on disposable three-node storage,
capture per-request dependencies and counters before and after attach, and
validate ordering before claiming a cause or fix. The earlier
[FILE_THIN resync stall](drbd_canary/thin_resync.md) showed socket backpressure;
it did not establish this same mechanism.

## Follow-up

- Verify CLI Proxy's real credential operations; investigate or reauthorize the
  Claude credential with the invalid refresh token. Decide retention of the
  sensitive temporary recovery copies.
- Complete the filer-wide reference check for the preserved SeaweedFS record,
  verify reads through the collector's mount, and decide recovery-copy
  retention. Investigate the physical NVMe and node with OVH.
- Investigate the misleading database health signal, stuck Talos shutdown,
  and incident alerting. Preserve the exact incident evidence separately.
