# OVH NVMe dropout and DRBD resync stall — 2026-11-20

**Status: open.** A data NVMe stopped responding, and reattaching its DRBD
volumes after a node reboot restored most services. One DRBD resync remained
stalled, and a SeaweedFS volume copy reported an index/data inconsistency.

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

At the operator's request, cleared LINSTOR's `DrbdOptions/SkipDisk` on the
affected resources after confirming their backing logical volumes existed.
Database instances mounted and rejoined their primaries. Three idle primaries
needed the documented [CNPG recovery nudge](../docs/cnpg_conventions.md#gotcha-replica-stuck-01-after-failover)
to advance WAL past the standby recovery point. All CNPG clusters subsequently
reported their configured instances ready; an Authentik standby's replay
position matched its primary. Casino's login route reached the Authentik flow;
an authenticated login was not tested.

One returned SeaweedFS copy reported an index pointing beyond its data file and
was marked read-only. Two other locations advertised writable copies with
matching size and counts. Those status reports do not establish content
integrity. Preserve the damaged copy and verify the others before repair.

## DRBD resync ordering wait

After its former diskless primary reattached its old local disk, a replicated
CLI Proxy state volume stayed `Inconsistent` at 0% resync. The source held
thousands of resync reads in `dagtag_wait`; the source and target remained
connected. The target's resource ordering counter was much higher than the
last tag the source had received from it. Small direct reads of the underlying
logical devices completed, so those probes did not reproduce a disk read stall.

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

Reproduce the diskless-primary reattachment on disposable three-node storage,
capture per-request dependencies and counters before and after attach, and
validate ordering before attempting a production remedy. The earlier
[FILE_THIN resync stall](drbd_canary/thin_resync.md) showed socket backpressure;
it did not establish this same mechanism.

## Follow-up

- Check the stalled DRBD resync and verify CLI Proxy's persistent state and
  real credential operations before claiming redundancy is restored.
- Assess SeaweedFS copies and application data before repairing the read-only
  copy; investigate the physical NVMe and node with OVH.
- Investigate the misleading database health signal, stuck Talos shutdown,
  and incident alerting. Preserve the exact incident evidence separately.
