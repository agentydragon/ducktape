# Piraeus block storage

Piraeus (LINSTOR + DRBD) supplies the cluster's local and replicated block
storage on Talos. One Flux-owned operator and one LINSTOR cluster are declared
in [`cluster/cdk8s/piraeus.py`](../cdk8s/piraeus.py); every OVH node and the
OptiPlex run a satellite with an LVM_THIN pool. Node maintenance, evacuation and
reimaging follow the [node runbook](runbooks/piraeus_node_roll.md).

## Disk ownership

```text
System disk: Talos boot/state + EPHEMERAL (+ ETCD on control planes) + CRI + KUBELET + LOG
Data disk:   whole device -> Piraeus VG (piraeus_ssd or piraeus_hdd) -> thin pool `data`
```

Talos owns the system disk; its split volume sizes per role live in
`piraeus_talos_node_layouts` and `piraeus_system_volume_sizes`
([`ovh-nodes.tf`](../terraform/main/ovh-nodes.tf)), and the OptiPlex's in
[`home-nodes.tf`](../terraform/main/home-nodes.tf) (one disk, with a raw
`r-piraeus` partition instead of a data disk). Piraeus initializes the whole data
device from the by-id path in its per-node satellite configuration
(`source.hostDevices`), with 4 GiB thin metadata, a 4 GiB spare and 64 KiB chunks.
There is no local-path or Talos user volume on a data disk. System volumes never
depend on a Piraeus PVC.

LINSTOR creates thin pools at `100%FREE`; creation arguments do not resize an
existing pool, and automatic pool growth is not enabled. Capacity grows by
adding pools on other nodes and moving volumes.

**Gotcha:** creating the 4 GiB metadata and spare on an HDD can exceed LINSTOR's
default external-command wait. HDD satellites set `ExtCmd/WaitTimeout=180000`.

## Storage classes

| Class                       | Pool  | Copies | Access                                                                                             |
| --------------------------- | ----- | -----: | -------------------------------------------------------------------------------------------------- |
| `piraeus-ssd-local`         | `ssd` |      1 | Strict-local: the PV pins its node; remote access disabled                                         |
| `piraeus-hdd-local`         | `hdd` |      1 | Strict-local                                                                                       |
| `piraeus-ssd-2-autodiskful` | `ssd` |      2 | Synchronous copies on distinct OVH nodes, diskless attach in-zone, auto-diskful, quorum suspend-io |

All are WaitForFirstConsumer, Retain and expandable; none is the cluster default.
The strict-local classes keep the default DRBD layer and rely on the
application's own replication (CNPG, Seaweed, ClickHouse, Valkey, Mimir/Loki
peers) or backups for durability. Do not add a second Piraeus copy under an
application that already replicates.

The replicated class keeps copies and attachments within `hil-ovh`, never across
the home/OVH link. Node-loss failover, resync and expansion are not yet accepted
for it; see the [production plan](plans/piraeus_production.md).

## Capacity policy

Aggregate logical PVC capacity may exceed a pool's physical thin-data size,
provided each PVC's size is bounded and actual pool use is monitored. Logical
capacity is not a reservation. When a pool's provisioned total exceeds its
physical capacity, track the excess and its exit plan (rebalancing, moving
workloads) in an issue; low current use does not close it. The live HDD case is
[#11786](https://github.com/agentydragon/ducktape/issues/11786).

Monitor thin data and metadata independently of PVC sizes. Pool metrics come
from LINSTOR plus the per-node collector in
[`piraeus_metrics.py`](../cdk8s/piraeus_metrics.py); the accounting contract is in
[the observability notes](../debug/piraeus_migration/observability.md). Before
evacuation or node maintenance, require fresh samples and the agreed headroom.

## Control plane and networking

- Satellites use host networking and advertise their Nebula addresses; DRBD
  replication uses TCP `7900-7999`. Switching a satellite back to the Pod network
  requires resetting DRBD paths or rebooting every involved node.
- Internal and API TLS come from cert-manager. Controller-to-satellite traffic is
  mTLS on `3367`; CSI and API clients use mTLS on `3371`, the only port the
  controller's NetworkPolicy admits. Plain HTTP `3370` serves health probes only.
- DRBD Reactor metrics are patched to `127.0.0.1:9942` and scraped by the local
  Alloy DaemonSet; the chart's satellite PodMonitor is removed.
- The LINSTOR datastore is Kubernetes. The controller, CSI controller and
  affinity controller run on prepared OVH nodes, independent of any one pool.
- The HA controller runs; the NFS server is disabled.

**Gotcha:** LINSTOR places diskless tiebreakers on any satellite, including a
cordoned node about to be wiped. Fence a node before maintenance with
`linstor node set-property NODE AutoplaceTarget false`, and clear it afterward
with `linstor node set-property NODE AutoplaceTarget`.
