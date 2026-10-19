# Canary execution observations

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

The node's historical Terraform `resolved_apply_mode=staged` is not evidence of
a currently pending configuration. Talos reports only the active `v1alpha1`
machine-config resource, with no `persistent` resource. The pinned Talos source
sets that resource for staged writes and omits it when loading configuration
solely from STATE during boot. STATE is mounted on demand, explaining why a
direct read of `/system/state/config.yaml` returned NotFound. No empty read result
was used as a machine configuration.

No Piraeus pool or test PVC has been created at this point. The 50Gi sparse probe
is separate from the 1Gi block migration fixture: a full resync of the large
logical device must not be attempted before its sparse-resync behavior is known.
