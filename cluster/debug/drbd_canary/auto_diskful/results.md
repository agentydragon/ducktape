# Auto-diskful observations, 2026-10-19 UTC

The dense raw-Block test passed in both directions, including writes during
synchronization. The separate sparse raw-Block test also passed without tuning
or a reconnect. All disposable test resources were cleaned up afterward.

## Configuration

Applied [fixtures.yaml](fixtures.yaml) explicitly, outside Flux ownership. The
existing provider, class, Talos preparation and disk layouts were unchanged.
Both prepared workers were Ready, LINSTOR ONLINE, provider Pods Ready, and
canary directories empty. The pools had about 1.75TiB and 673GiB free.

Verified the separate class's effective resource group `auto-diskful-canary`:
`place_count: 1`, pool `local-canary`, `DrbdOptions/auto-diskful: 1` and
`auto-diskful-allow-cleanup: true`. Remote access was enabled. No manual replica
creation, disk toggle, source-copy removal, reconnect or resync tuning was used
during moves. Old-copy deletion was performed by LINSTOR itself.

The dense claim was 1Gi Block, PVC UID
`474e19f8-5da6-41fe-9cc1-a7eb3d0cdda5`, CSI handle
`pvc-474e19f8-5da6-41fe-9cc1-a7eb3d0cdda5`. Both stayed unchanged across moves.
Generated settings included protocol C and 4096-byte `rs-discard-granularity`;
the earlier canary's 1MiB tuning was not used.

## Dense moves

Fully wrote and CRC32C-verified all 1GiB and recorded a full-device SHA-256.
Stopped the test Pod and recreated it with the other hostname selector, using
the existing PVC. CSI created the diskless destination automatically.

| Event (UTC)                                       | Forward to ovh-ns103656 | Reverse to ovh-ns102453 |
| ------------------------------------------------- | ----------------------- | ----------------------- |
| Stop Pod                                          | 04:30:33                | 04:33:26                |
| Destination Ready observed                        | 04:31:14                | 04:34:19                |
| Local synchronization                             | 04:32:17 sample         | 04:35:15 sample         |
| Only destination copy, Primary/UpToDate, no peers | 04:32:44                | 04:35:48                |

The controller logged the forward automatic disk toggle at 04:32:08.812, about
one minute after activation on the destination. A direct 64MiB CRC32C read
passed while the destination was Primary/Diskless. The Pod kept running while
synchronization progressed. After cleanup, its full-device SHA-256 matched the
original and a full 1GiB CRC32C read passed. The old directory returned to zero
allocation and the new copy occupied about 1GiB.

For the reverse move, initialized the final 64MiB with 4KiB CRC32C blocks,
preserving the first 960MiB and its original SHA-256. A direct random writer
with `fsync=1` ran from 04:34:17 to 04:36:08, spanning remote access, local sync
and single-copy operation. It wrote 16,265,216 bytes over 110 seconds and
completed 3,971 fsyncs with fio error zero. Afterward the immutable 960MiB hash
matched and both that region and the rewritten tail passed CRC32C verification.
PVC UID and CSI handle remained unchanged. The old directory again returned
to zero allocation.

Stop-to-Ready observations were 41 and 53 seconds, including normal termination,
attachment, runtime package installation and sampling delay. Locality and
cleanup completed about 90 seconds after readiness. These are small shared-HDD
observations, not downtime or migration guarantees. No source-node outage or
host reboot was injected; this does not establish recovery from losing the
only disk-backed copy.

## Sparse move with an idle destination

Deleted the dense test volume after detachment and confirmed both directories
empty. Created a fresh 1Gi Block claim, PVC UID
`f5cd82e5-7363-4f93-991c-263596acd7c5`, with only a 64MiB CRC32C payload. Its
source image occupied 65,800KiB, about 64.3MiB, including metadata.

Stopped the first-worker Pod at 04:38:02 and recreated it on the second worker
with the same PVC. Destination readiness was observed at 04:38:42. Issued no
application data reads or writes there during the delay; readiness runs only
`fio --version`. The controller automatically initiated the disk toggle at
04:39:37.900. The copy synchronized and the old copy was automatically removed
by the 04:39:59 observation. Thus application traffic did not need to reach any
threshold to start copying.

Afterward the full 1GiB SHA-256 matched the original, including the unwritten
region, and the 64MiB payload passed CRC32C verification. A separate ten-second
direct random-write/fsync job completed with zero errors while the destination
was the only copy, Primary/UpToDate with no peer connections. PVC UID and CSI
handle were unchanged. The source directory was empty and the destination
remained at 65,800KiB allocation, so this resync preserved thin allocation.

The earlier 4GiB filesystem sparse-resync stall remains unexplained. This 1GiB
raw-Block idle pass used generated 4KiB discard granularity and did not reproduce
it; size, filesystem layout, writer placement and workload differ. It does not
establish that the earlier stall is fixed or that larger migrations are safe.

## Implication

For these canaries, changing only consuming Pod placement was sufficient to
obtain remote access, automatic local copying, and automatic return to one data
copy. No additional migration controller was needed. The timer is driven by
LINSTOR observing an in-use diskless resource, not by bytes transferred. The
same node may already store unrelated volumes locally.

This remains an evaluation on two prepared Talos workers. NixOS interoperability,
larger sparse filesystem moves, capacity policy and failure handling remain
adoption gates. No production workloads, including cliproxyapi, were migrated.

## Cleanup

Cleanup completed at 04:41:59 UTC. After stopping each test Pod, verified the
recorded PVC/PV/CSI identities, known Unused/UpToDate state, closed devices and
absence of Kubernetes attachments before deleting its claim, retained PV and
matching backend resource definition. Deleted the empty test resource group,
`auto-diskful-canary` class and `drbd-auto-diskful-tests` namespace.

Both canary directories returned to zero allocation, both satellites reported
zero DRBD resources, and LINSTOR listed no resources. Both workers remained
Ready and schedulable; provider Pods were Ready with no new restarts and the
cluster's Applied, Available and Configured conditions were True. The existing
provider and original opt-in class remain installed. Local pre-commit checks
passed for the fixture and documentation.
