# Canary execution observations

Core storage acceptance passed on the two Talos workers: thin raw Block and
Filesystem volumes, one-copy writes, enforced size, expansion/discard, restart
and reboot persistence, and same-PVC moves in both directions. The final
stopped-Pod and online moves matched full-device source/destination hashes.

The generated FILE_THIN defaults stalled on the first sparse filesystem resync.
A volume-specific 1MiB discard-request size and reconnect recovered it; later
copies with that setting passed. Resolve this default/tuning boundary and test
NixOS integration before fleet adoption. `wyrm2` was not changed or rebooted.

## 2026-10-19 UTC: host preparation

- Host preparation PR #11012 merged as `18186dd063ef971587a5b677178a51369a64121b`.
- Registered the new DRBD schematic in Terraform state. The saved registration
  plan created only `talos_image_factory_schematic.drbd_canary`.
- The normal targeted machine-config plan was rejected: it included replacement
  of all six `null_resource.install_talos_kimsufi` instances because their old
  `schematic_id` triggers differ from the current shared schematic. Those
  installation scripts were not invoked.
- The generated first-worker configuration also differed from live Nebula
  extension configuration (`documents[3].configFiles[3].content`). That drift
  was excluded from the canary configuration.
- Prepared a temporary resource-only Terraform root using the existing `main`
  state backend and resource address. Its desired input preserves the live
  multi-document configuration and copies only the committed canary image,
  modules, and label. The saved plan updates only the selected
  `talos_machine_configuration_apply.kimsufi` instance. This does not reconcile
  the unrelated full-root drift; future full-root applies still need review.
- Checked both ClickHouse replicas using `system.replicas` and
  `system.replication_queue`: each had 14 replicated tables, two active replicas,
  no queued items/errors, and zero maximum delay. The operator's incomplete
  `hostsWithReplicaCaughtUp` status was not evidence of replica lag.
- First-worker SeaweedFS gate: 348 hosted volumes, none below their required
  copy count and none with only one copy. No volume repair/deletion was run.
- Switched `mcp-oauth-state-db` from instance 2 on the first worker to healthy
  instance 1 on the second worker; confirmed two healthy instances afterward.
- Matrix Synapse's bound claim uses `seaweedfs-ovh` RWX. An initial inference
  from a similarly named local PV was incorrect; it has no local-PVC dependency
  on the second canary worker.

Private machine configurations, Terraform plans/state, and raw logs stay outside
the repository. No Piraeus provisioning or relocation acceptance result is
established by these preparation observations.

### First worker result

`ovh-ns102453` booted the intended schematic and DRBD extension. The image
installer reported exit code 0, but the v1.14.2 upgrade client remained open
without initiating reboot. After verifying completion in machined's log, stopped
that client and issued a normal explicit reboot without repeating installation.

Boot was delayed by XFS quota checks: `/dev/sda5` checked from approximately
01:33:53 to 01:39:52 UTC, followed by the HDD data filesystem. Both mounts already
reported `projectQuotaSupport=true` before the roll; the canary did not change
quota configuration. CRI subsequently logged a 10-second watchdog warning around
opening its 100,757,504-byte Bolt metadata database. Its own PID held the exact
database inode lock, separate from system containerd's locks, and process I/O
continued. CRI recovered without a restart, signal, or database modification.
The exact slow operation inside database initialization was not captured; a
freelist reconstruction is a hypothesis, not an established cause.

The reviewed configuration-only saved plan updated one existing resource,
with `resolved_apply_mode=auto`. Verified:

- DRBD module version `9.3.3`, loaded `drbd_transport_tcp`, and helper `disabled`.
- Loop and DRBD block-device majors registered (`7` and `147`).
- Original XFS `/dev/sdb` mounted at `/var/mnt/local-path-ovh-hdd`.
- Original Kubernetes Node UID, hostname, InternalIP, and pod CIDR preserved.
- Cilium Ready; kubelet and CRI healthy; Nebula running.
- Node Ready and uncordoned around 01:53 UTC. Local-PVC workloads were Ready by
  01:58 UTC, and all 21 CNPG clusters had their expected ready-instance counts
  by 02:00 UTC. Fresh ClickHouse, SeaweedFS and Flux checks passed before
  preparation for the second worker began.

### Second worker preparation

Before draining `ovh-ns103656`, moved the `mcp-oauth-state-db` and `ntfy-db`
primaries to the first worker and verified both instances healthy with streaming
replication. The former MCP primary's manager needed automatic liveness recovery
after its clean PostgreSQL shutdown; it recovered without a manual restart or
data changes. The precise manager failure remains unproven.

The Claude session-sync database's planned switchover waited for its PostgreSQL
archiver to drain an existing WAL backlog. Its shutdown checkpoint completed in
about seven seconds; the archive queue then decreased from 355 pending segments
at 02:20:51 UTC to two at 02:25:45 UTC, with matching advances in the archiver's
segment name. The shutdown command was `pg_ctl -m fast stop -w -t 3600`; the
replacement standby was caught up. This was active archive work, not a reason
to bypass graceful shutdown or drain the worker while its primary was stopping.
The switchover completed around 02:26 UTC with both instances healthy, no archive
backlog, and zero streaming-replication lag.

The node's historical Terraform `resolved_apply_mode=staged` is not evidence of
a currently pending configuration. Talos reports only the active `v1alpha1`
machine-config resource, with no `persistent` resource. The pinned Talos source
sets that resource for staged writes and omits it when loading configuration
solely from STATE during boot. STATE is mounted on demand, explaining why a
direct read of `/system/state/config.yaml` returned NotFound. No empty read result
was used as a machine configuration.

### Second worker host result

After fresh checks of all 21 CNPG clusters, the first worker's workloads and the
304 target-hosted SeaweedFS volumes, drained `ovh-ns103656` normally. Drain
completed around 02:30:53 UTC without forced deletion or eviction bypass.
Used the supported legacy upgrade API with data preservation enabled to avoid
the first worker's lifecycle-install stream hang. The new boot ID and intended
schematic/DRBD extension were verified through the public Talos API.

The configuration-only saved plan updated exactly the existing second-worker
resource (`0 added, 1 changed, 0 destroyed`). A fresh comparison before apply
showed only the permitted image, kernel-module settings, and canary label.
DRBD `9.3.3`, TCP transport and helper `disabled` were verified afterward.
The active and persistent Talos configurations compare equal.

XFS quota checking took approximately 02:32:39–02:36:44 UTC for `/dev/sda5`
and three seconds for `/dev/sdb`. CRI metadata initialization subsequently
delayed startup and recovered without intervention; process I/O advanced from
121,847,296 to 150,170,112 physical read bytes between observations. The
original XFS `/dev/sdb` data mount, Kubernetes Node UID, hostname, addresses and
pod CIDR were preserved. Node Ready, Cilium Ready and loop/DRBD block majors
passed before uncordoning around 02:44 UTC. Replica restoration is a separate
gate before Piraeus deployment.

That restoration gate passed at 02:46–02:47 UTC: all 21 CNPG clusters had their
expected ready-instance counts, all six previously active node2 local-PVC
consumers were Ready, all six SeaweedFS volume servers were Ready, both canary
HDD per-volume checks passed, ClickHouse had two healthy replicas and Keeper
had all three members Ready. The proposed listener/replication ports were free
on both nodes before deployment.

An optional post-apply Terraform state read failed twice because the BuildBuddy
CLI's BES proxy could not resolve its upstream. No state was modified by that
read, and no resolver or workstation configuration was changed. The successful
saved-plan apply and the live Talos checks above are separate verified evidence.

### Provider deployment

Reviewed deployment PR #11029 at exact green head
`0e7e40665ab463a5be621059f5b5d8ad64c0c708`, then merged it as
`c1dddfc8b9a8d7e49cb8171281d8056c9c0d7cac` after both workers passed
preparation and recovery. Flux fetched that commit. Provider readiness, network
checks and PVC acceptance remain separate live checks.

The initial test Pods stopped during package installation: the broad `util-linux`
package pulled ownership-sensitive helpers, and the fixtures deliberately drop
all capabilities. Removed that unneeded package; the I/O exercises require only
`fio` and `coreutils`. Reused the existing PVCs when recreating those Pods.

### Provider runtime and volume checks

The operator, controller, affinity controller, CSI controller/node, and both
satellite/reactor Pods were Running and Ready in the post-restart snapshot, with
zero container restarts. LINSTOR returned both canary nodes and their active
satellite interfaces: SSL on TCP `3367` at Nebula addresses `10.42.0.15` and
`10.42.0.13`. Configured LINSTOR client queries returned node and pool data.
An unauthenticated TLS request was rejected; a test Pod could reach controller
TCP `3371`, while plaintext TCP `3370` was denied. Controller-local HTTP health
checks passed. Both reactor metrics listeners bound only to `127.0.0.1:9942`.

The FILE_THIN pool exposed the expected logical sizes: `4Gi` filesystem, `1Gi`
raw Block, and `50Gi` sparse raw Block. A sampled pool-directory allocation after
the initial writes reported `270568`, `131600`, and `153552` allocated 512-byte
sectors for those images (about `132MiB`, `64MiB`, and `75MiB` respectively;
`284532736` bytes total). This is one sampled directory, not a per-node quota
measurement. FILE_THIN uses the existing filesystem, so the `20Gi` per-node
stop threshold remains an operator-enforced limit, not a quota. The `50Gi` sparse
volume received only a small write probe; it was not fully resynced.

The `1Gi` raw Block exercise wrote the full `1073741824` bytes and passed
CRC32C readback. A write beyond the end failed with `ENOSPC`, as expected.
Twenty-second direct-I/O `4KiB` random-write/fsync jobs over a `64MiB` range
completed with fio error `0` for both the raw Block and filesystem baselines.
The raw Block fsync job also passed during a controller restart; the filesystem
fsync job passed during an active satellite restart. Subsequent CRC32C reads
passed for the Block, sparse Block, and filesystem volumes. fio printed a cache
invalidation permission warning on raw-device runs; the direct-I/O jobs and
explicit CRC readbacks still passed.

### Online replica synchronization

Inspection of the pinned LINSTOR v1.35.2 source showed that
`toggle-disk --migrate-from` rejects an active source with `InUse`; we did not
invoke that shortcut. The test instead added a diskful copy on the second
canary node and synchronized it while the original stayed Primary and the raw
Block writer continued. During an `11`-second connection interruption, the
source remained open, Primary, and UpToDate while the peer was StandAlone at
about `65.8%` in sync. After reconnect, DRBD reported the peer UpToDate and
Established with zero out-of-sync bytes. The five-minute CRC32C random-write/
fsync stream completed with fio error `0` and wrote about `56.4MB`.

After the writer finished, a full `1Gi` CRC32C read passed on the source.
Deleted only its Pod and waited for detachment: both LINSTOR copies were
known Unused, the source device was closed, and the destination was UpToDate.
Then removed the old source resource and recreated the Pod on `ovh-ns103656`.
The Pod was Ready at `03:18:37 UTC`, 88 seconds after the stop began.
This includes manual checks, Pod termination and runtime package installation;
it is not a minimum achievable cutover time.

The PVC UID and CSI handle remained unchanged. The affinity controller recreated
the PV to update its immutable node affinity; its UID changed, and its final
affinity contained only `ovh-ns103656`. Full `1Gi` CRC32C readback passed there.
DRBD reported Primary, UpToDate and no peer connections, restoring one local
disk copy without remote write acknowledgements.

The `4Gi` filesystem volume initially stalled during sparse resync with the
generated `4KiB` discard-request granularity. Stopping its writer did not clear
the stall. A volume-specific `1MiB` granularity setting followed by an ordinary
resource disconnect/reconnect resumed steady progress; a second five-minute
writer completed with error `0` while synchronization continued. The pinned
source and measurements are in [thin_resync.md](thin_resync.md). This is an
adoption gate for the FILE_THIN configuration; the generated defaults have not
passed sparse migration unchanged.

After both filesystem copies were UpToDate, verified the source data, stopped
only its Pod, checked both copies Unused and the source device closed, then
removed the old copy. The destination Pod became Ready at `03:34:38 UTC`,
91 seconds after cutover began. Its `64Mi` CRC32C readback passed, PVC UID and
CSI handle were unchanged, PV affinity contained only the second worker, and
DRBD returned to one Primary/UpToDate copy with no peer connections.

### Expansion and discard

The mounted raw Block claim expanded online from `1Gi` to `2Gi`. Kubernetes
reported the new capacity, the actual device exposed `2147483648` bytes, and
a write/read CRC32C probe passed over `64Mi` starting at offset `1Gi`.
Discarding exactly that new probe region reclaimed `67108864` physical bytes;
the region read back as zeroes and the original full `1Gi` CRC32C read still
passed. Only the recorded disposable raw device was discarded.

The mounted ext4 claim expanded online from `4Gi` to `5Gi`; its actual filesystem
size increased from `4143677440` to `5200576512` bytes and its payload CRC32C
check passed. A separate `64Mi` file passed write/read verification and was
deleted. Ran `fstrim` only on the CSI mount whose Pod UID, PVC name, device
`/dev/drbd1000` and filesystem type matched this test. The backing image's
allocation fell by exactly `67108864` bytes, and the payload CRC32C check still
passed. `fstrim` reported about `4.8Gi` submitted; that is not the amount of
physical space reclaimed. After move, expansion and trim, this `5Gi` logical
image occupied `146030592` physical bytes (about `139MiB`).

### Reboot persistence

Stopped the raw Block and filesystem Pods after checksum verification and
confirmed both local resources were Unused and UpToDate. Fresh production gates
showed all 21 CNPG clusters healthy, no primary on the second worker, safe
SeaweedFS copies, healthy ClickHouse/Keeper and available disruption budgets.
Cordoned at `03:37:34 UTC`, drained normally by `03:38:59 UTC`, then requested
a normal reboot. PDBs held the remaining staging replicas until their off-node
replacements became Ready; no eviction bypass was used.

XFS quota checking and CRI initialization delayed the return. Both data mounts
were established after quota checks completed around `03:44:48–03:44:51 UTC`.
CRI then read its `117501952`-byte Bolt metadata database: process read bytes
advanced from `77152256` through `100999168` to `118477824`, and logs advanced
to serving gRPC around `03:48:58 UTC`. It recovered without signals, restarts
or database changes. This startup delay is separate from volume persistence.

The new boot ID, Node Ready, Cilium Ready, DRBD `9.3.3`, helper `disabled`,
the pool's original `/dev/sdb` XFS mount, and both single-copy UpToDate resources
passed before uncordoning at `03:52:21 UTC`. Recreated the two Pods using the
existing PVCs. Full `1Gi` raw CRC32C and the filesystem payload CRC32C passed;
PVC UIDs were unchanged. Data verification completed at `03:53:15 UTC`.

While the second worker was reported OFFLINE, the first worker's single-copy
50Gi sparse probe also passed its bounded `64Mi` write, fsync and CRC32C readback.
It was never replicated or fully filled.

### Return move and byte-for-byte verification

Moved the expanded `2Gi` raw volume back from the second worker to the first
with its Pod stopped for the entire copy. Both copies were UpToDate and known
Unused before source deletion. Full-device SHA-256 values matched before and
after transfer; the original `1Gi` CRC32C check also passed. The same PVC and
CSI handle survived, final affinity selected only the first worker, and its
single local copy passed another synchronous-write exercise. The stop-to-Ready
interval was `03:53:45–03:55:15 UTC` (90 seconds), including synchronization
through `03:54:41 UTC` and manual/tool overhead.

Then repeated the forward move with a live writer and full-device SHA-256
comparison. The writer completed `12,324,864` bytes over about 49 seconds with
fio error `0` while the added copy synchronized. After quiescing the writer,
hashed all `2Gi`, detached the Pod, verified both copies Unused/UpToDate and
the source closed, then removed the old copy and started the Pod on the second
worker. The full destination hash matched. Cutover to Pod Ready took
`03:57:00–03:57:32 UTC` (32 seconds); destination hashing followed readiness.
PVC UID and CSI handle stayed unchanged, and DRBD again had one Primary/
UpToDate copy with no peers. This hash comparison supplements the CRC checks:
an older internally consistent block must not pass as the latest data.

Both final moves used protocol C, `32M` resync rate, and the volume-specific
`1MiB` discard granularity. These short shared-HDD observations do not establish
production downtime bounds. Asynchronous protocol A was not tested.

At `03:55:57 UTC`, the independent production restoration gate passed: both
canary workers were Ready and schedulable, all 21 CNPG clusters were fully
ready, all six second-worker local-PVC consumers were Ready, SeaweedFS's 304
target-hosted volumes had their required copies, ClickHouse/Keeper were healthy,
and the Piraeus resources were Ready. The two previously offline roaming nodes
remained offline. No `wyrm2` host changes were made.

### Synchronous-write observations

These are short samples on shared HDDs, not a provider benchmark. Every job
used direct `4KiB` random writes, `fsync=1` and a `64Mi` working region for
20 seconds, with fio error `0`.

| Condition                                                  | Write IOPS | Mean fsync | p99 fsync |
| ---------------------------------------------------------- | ---------: | ---------: | --------: |
| Raw Block, first worker, one copy                          |       85.5 |   11.61 ms |  30.02 ms |
| Filesystem, first worker, one copy                         |       26.5 |   37.59 ms |  96.99 ms |
| Raw Block during controller restart                        |       86.5 |   11.48 ms |  32.90 ms |
| Filesystem during satellite restart                        |       26.5 |   37.58 ms |  99.09 ms |
| Raw Block after move, second worker, one copy              |       32.6 |   30.55 ms |  53.22 ms |
| Raw Block after return, first worker, one copy             |       75.7 |   13.02 ms |  39.58 ms |
| Raw Block after final online move, second worker, one copy |       48.7 |   20.38 ms |  47.97 ms |

The filesystem also passed a second-worker single-copy post-reboot fsync job
with error `0` (11.3 IOPS, mean 84.45 ms, p99 259.00 ms) while another disposable
volume was copying from that worker's shared HDD. This is a contention sample.

### Adoption boundaries

The canary demonstrates thin Block/Filesystem provisioning, logical capacity
enforcement, online expansion, space reclamation, reboot persistence, and
same-PVC relocation with both online initial synchronization and a stopped-Pod
fallback. Steady state returned to one local copy, including successful writes
while the other storage host was offline. Moves remain an explicit LINSTOR
add-copy, quiesce, detach and source-removal procedure; no declarative PVC move
workflow was implemented.

Before fleet adoption, resolve or reproducibly explain the sparse-resync stall
with generated defaults, declare and retest any chosen tuning, and validate a
NixOS satellite plus Talos/NixOS moves. SSD/HDD placement policy, dedicated
capacity/layout, oversubscription monitoring, snapshots and asynchronous moves
remain separate decisions or tests. The canary class remains opt-in and limited
to the two prepared Talos workers; it is not the universal fleet class yet.

### Cleanup

Cleanup completed at `03:59:21 UTC`. Rechecked the three recorded PVC UIDs,
PV/CSI handles and Retain policies, stopped their Pods, waited for all three
resources to be Unused/UpToDate and for Kubernetes attachments to disappear,
then removed the claims, retained PVs and matching LINSTOR resource definitions.
Both satellites reported no DRBD resources, and both canary pool directories
reported `0` allocated bytes with no backing images. Deleted the disposable
test namespace, including the API probe and exercise ConfigMap. No production
PVC or disk layout was changed.

Piraeus, its managed metadata/certificates, the opt-in class, and the two workers'
DRBD preparation remain installed for further evaluation. The task's temporary
Terraform-state port-forward was stopped.

Post-cleanup checks still showed all 21 CNPG clusters fully ready, both canary
workers Ready and schedulable, and LINSTOR Applied/Available/Configured with
zero volumes. The class remained nondefault, WaitForFirstConsumer, expandable
and Retain.
