# Kubernetes VM Platform Options

Question: what existing solution should we use if we want to run VMs inside the
cluster, with some combination of snapshotting, suspend/resume, and not losing
the VM when a node goes down?

## Summary

Use KubeVirt, or a KubeVirt distribution, if the goal is "real VMs managed as
Kubernetes resources."

For this cluster, the practical path is:

1. Trial upstream KubeVirt on the existing Talos/NixOS cluster with one
   disposable Linux VM.
2. Treat storage as the hard part. The current active storage classes are mostly
   local or region-local; they do not by themselves provide VM HA across node
   loss.
3. If VM HA/live migration becomes important, pick or revive a VM-suitable
   replicated storage layer before putting anything important on it. The likely
   candidates are Longhorn or Rook/Ceph, not OpenEBS LVM local volumes and not
   SeaweedFS CSI.
4. Do not bet on a generic "suspend to disk and resume anywhere" feature in
   vanilla KubeVirt. KubeVirt has pause/unpause, disk snapshots, live migration,
   and restart-on-failure semantics, but not a normal end-user hibernate-style
   save of RAM+CPU state to durable storage for later resume.

The short version: KubeVirt is the right existing add-on. Harvester/SUSE
Virtualization is the right packaged appliance if we want a separate
VMware-like HCI platform. Virtink is interesting but too immature for this. Kata
and firecracker-containerd are VM-backed container runtimes, not persistent VM
platforms.

## Important Semantics

There are three different "do not lose the VM" meanings:

| Scenario                                            | What is realistically possible                                                                                                                              |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Planned node maintenance                            | Live migrate the running VM before draining the node, if the VM's disks and network support migration.                                                      |
| Sudden node death                                   | Restart the VM elsewhere from replicated storage. RAM and active TCP/process state are gone unless a platform has continuous checkpointing/fault tolerance. |
| User wants to stop using resources and resume later | Either shut down and later boot from disk, or use a hypervisor save/suspend-to-disk feature. This is not a normal KubeVirt VM lifecycle primitive today.    |

For any platform, if the physical node dies abruptly, it cannot preserve the RAM
state that only existed on that node. The best standard answer is replicated
block storage plus VM restart. Live migration only helps before a planned
eviction or when the source node is still healthy enough to participate.

## Current Cluster Fit

The current storage table in `cluster/README.md` says all storage is
region-local, with:

- `local-path-proxmox`, `local-path-ovh-{hdd,ssd}`, `local-path-home-ssd`:
  node/region-local host storage.
- `lvm-proxmox-ssd` and `lvm-proxmox-hdd`: OpenEBS LVM LocalPV on Proxmox
  nodes.
- `proxmox-csi-retain`: not installed; Proxmox CSI is not part of the current
  storage platform.
- `longhorn`: not installed; any revival would be a fresh storage trial.
- SeaweedFS is present for object/S3 workloads and experimental CSI/FUSE use,
  but repo notes already say not to use it for core infra until recovery,
  upgrades, and node restarts are boring.

Implications:

- A VM on `lvm-proxmox-*` is good for fast local storage, but the VM is tied to
  the Proxmox node/VG. This does not solve "node died, restart elsewhere."
- A VM on `local-path-*` is even more node-coupled.
- There is currently no `VolumeSnapshotClass` in the cluster, based on existing
  `volsync-backup.yaml` comments and `rg` inspection. KubeVirt snapshots need a
  snapshot-capable CSI class.
- If live migration/HA is a requirement, the missing foundation is a real VM
  storage layer: a newly evaluated Longhorn deployment, Rook/Ceph, or an
  external NAS/SAN/NFS design with acceptable performance and failure semantics.

## Option 1: Upstream KubeVirt

What it is: a Kubernetes add-on that introduces `VirtualMachine` and
`VirtualMachineInstance` CRDs and runs each VM through QEMU/KVM under Kubernetes
control.

Strengths:

- Best-supported upstream answer for persistent VMs inside an existing
  Kubernetes cluster.
- Installs as a Kubernetes operator/add-on rather than replacing the cluster.
- Has normal VM lifecycle primitives: start, stop, restart, console, cloud-init,
  disks, CDI image import, VM snapshots/restores, and live migration.
- Can run on Talos. SideroLabs has a guide specifically for installing KubeVirt
  on Talos.
- KubeVirt's own install docs require recent Kubernetes, privileged DaemonSets,
  and `/dev/kvm` availability on VM-capable nodes.

Limitations:

- Uses QEMU/libvirt, so it is heavier than Firecracker or Cloud Hypervisor.
- Pause/unpause is not suspend-to-disk. KubeVirt documents pause as libvirt
  `virDomainSuspend`: CPU and IO stop, but VM memory remains allocated on the
  host.
- KubeVirt's memory dump feature is for diagnostics and is explicitly not a
  save/resume mechanism.
- VM snapshots are storage snapshots. KubeVirt uses Kubernetes
  `VolumeSnapshot` through CSI for persistent VM state; restore requires the VM
  target to be stopped.
- Live migration requires careful storage and networking. The current KubeVirt
  docs say PVC-backed VMs must use shared RWX access for live migration. During
  migration KubeVirt copies memory, and sometimes disk blocks, while the VM
  keeps running.
- Sudden node failure is restart/failover, not live migration. KubeVirt's
  `RunStrategy: Always` or `RerunOnFailure` can recreate a VM instance after an
  infrastructure failure, but only if storage can be attached safely elsewhere.
  KubeVirt also has its own virt-handler heartbeat and may take up to about five
  minutes to mark an unresponsive virt-handler/node path unhealthy.

Fit for this cluster:

- Good first trial if we are comfortable running privileged virtualization
  components.
- Best initial target would be a disposable Linux VM on a Proxmox/NixOS worker
  with `/dev/kvm`, not anything production-like.
- The trial should explicitly avoid promising node-failure durability until the
  storage layer is chosen.

Recommended trial shape:

1. Install KubeVirt and CDI into a non-critical namespace/slice.
2. Restrict VM scheduling to a known KVM-capable node pool. Start with `wyrm2`
   or another Proxmox/NixOS worker before trying OVH Talos nodes.
3. Create one small VM with cloud-init.
4. Validate console, SSH, shutdown/start, and deletion cleanup.
5. Add a snapshot-capable CSI path or confirm one exists before testing
   `VirtualMachineSnapshot`.
6. Only then evaluate live migration with a storage class that KubeVirt reports
   as migratable.

Verdict: best existing add-on for this cluster. The blocker is storage, not the
VM controller.

## Option 2: Harvester / SUSE Virtualization

What it is: a packaged HCI platform built on Kubernetes, KubeVirt, and
Longhorn. Harvester's current docs describe KubeVirt for VM management and
Longhorn for distributed block storage. It advertises VM lifecycle management,
live migration, backup, snapshot, and restore.

Strengths:

- Much closer to a "turnkey open-source VMware replacement" than upstream
  KubeVirt.
- Includes UI, VM images, networking, Longhorn storage integration, snapshots,
  backups, restore, and live migration flows.
- The storage story is integrated rather than left as a DIY KubeVirt
  prerequisite.
- Harvester docs validate several CSI drivers for VM use. Their third-party
  storage matrix explicitly calls out live migration and VM snapshot support by
  driver.

Limitations:

- It is an appliance/HCI distribution, not an add-on I would casually install
  into the existing Talos workload cluster.
- Adopting it likely means dedicating hardware to Harvester/SUSE
  Virtualization, then running VMs there, possibly with Rancher integration.
- It overlaps with, rather than simply extends, the existing cluster's
  infrastructure model.
- It still inherits the same fundamental semantics: live migration for planned
  movement, restart from replicated storage for hard node death.

Fit for this cluster:

- Best if the real goal is "I want a reliable VM platform in the homelab" more
  than "I want a few VMs as Kubernetes objects in this exact cluster."
- Not my first move if the goal is one or two small utility VMs.

Verdict: strongest packaged existing solution, but probably a separate platform
decision rather than a small cluster feature.

## Option 3: OpenShift Virtualization

What it is: Red Hat's supported KubeVirt distribution inside OpenShift.

Strengths:

- Mature enterprise packaging around KubeVirt.
- Good UI and operational documentation.
- Strong migration, storage, and support ecosystem if already using OpenShift.

Limitations:

- Requires adopting OpenShift, which is not aligned with this Talos/Flux cluster.
- Heavyweight for personal cluster use.

Fit for this cluster:

- Mostly useful as a reference architecture for how KubeVirt should be operated,
  not as a deploy target.

Verdict: not a practical option unless the cluster itself moves to OpenShift.

## Option 4: Virtink

What it is: a lightweight Kubernetes add-on for Cloud Hypervisor VMs. It avoids
QEMU/libvirt and has lower per-VM overhead. Its README says it is a Kubernetes
add-on for Cloud Hypervisor VMs and calls out lower memory footprint than
KubeVirt.

Strengths:

- Closer to the "microVM/lightweight" model than KubeVirt.
- Uses Cloud Hypervisor, which has native pause/snapshot/restore and live
  migration primitives at the VMM layer.
- Exposes a `VirtualMachine` CRD and supports VM power actions such as
  `PowerOff`, `Shutdown`, `Reset`, `Reboot`, `Pause`, and `Resume`.

Limitations:

- The project README still says the API may change without notice.
- Latest GitHub release visible from the repo page is `v0.17.0` from
  2024-12-11.
- README requirements list Kubernetes `v1.16` through `v1.25` and old
  cert-manager ranges, which is a red flag for a current cluster.
- Live migration is listed in the roadmap, not as a clearly stable feature.
- It is less battle-tested than KubeVirt and has a much smaller ecosystem.

Fit for this cluster:

- Interesting for lab experimentation with lightweight Cloud Hypervisor VMs.
- Not where I would put any important persistent VM.

Verdict: maybe worth a sandbox trial, not the recommended base platform.

## Option 5: Kata Containers

What it is: a Kubernetes RuntimeClass/container runtime that runs Pods inside
small VMs using QEMU, Cloud Hypervisor, Firecracker, or Dragonball depending on
configuration.

Strengths:

- Good fit when the actual workload is still a Pod/container but needs stronger
  isolation than runc.
- Integrates through RuntimeClass once the node runtime is configured.
- Supports multiple VMM backends, including Firecracker and Cloud Hypervisor.

Limitations:

- Not a persistent VM platform. The abstraction is still "Pod sandbox," not "VM
  with lifecycle, disks, console, migration, backup."
- Kata's own limitations doc says the runtime does not provide checkpoint and
  restore commands.
- Kata+Firecracker setup has extra devmapper/containerd requirements and does
  not expose raw Firecracker arbitrary VM snapshot/restore as a normal
  Kubernetes VM feature.

Fit for this cluster:

- Good for untrusted container isolation if that becomes the goal.
- Not good for "I want a VM that survives node churn and has snapshots."

Verdict: wrong abstraction for persistent VMs.

## Option 6: Raw Firecracker / firecracker-containerd / Flintlock / Ignite

What it is: Firecracker itself has excellent microVM snapshot/restore
primitives. It can pause a microVM, create full or diff snapshots, and later
load the VM in another Firecracker process. Firecracker docs also note important
requirements: snapshot files are trusted host state, memory files must be
treated immutable after load, guest clock/network/entropy need restore fixups,
and snapshots require compatible host hardware/software.

Strengths:

- Best primitive for fast warm microVM resume and fork-from-snapshot patterns.
- Much lighter than QEMU/KubeVirt.
- Firecracker full/diff snapshots are exactly the mechanism used by many
  serverless/sandbox systems.

Limitations:

- Raw Firecracker is not a Kubernetes VM platform.
- firecracker-containerd is for running containers inside Firecracker microVMs;
  it does not give us an end-user VM CRD with durable snapshots/migration.
- Flintlock/Ignite-style projects are either archived, thin orchestration
  layers, or not current enough to bet this cluster on.
- To make Firecracker satisfy the requested semantics, we would need to build or
  adopt an opinionated controller, storage layout, snapshot manager, networking
  model, and restore fixup layer. That is exactly the custom-manager direction
  the user said not to focus on.

Fit for this cluster:

- Keep as background knowledge and for specialized sandbox/dev-VM work.
- Do not use as the main "existing VM platform" answer.

Verdict: best primitive, but not the existing product we want for this question.

## Storage Choices If We Choose KubeVirt

The storage decision determines whether node-failure recovery and live migration
are real or mostly aspirational.

| Storage option              | VM fit                               | Notes                                                                                                                                                                                            |
| --------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Current OpenEBS LVM LocalPV | Good local performance, poor HA      | Node/VG-local. Fine for disposable VMs and experiments. Not enough for node-down recovery.                                                                                                       |
| Current local-path classes  | Poor for VM HA                       | Host-path storage. Useful for simple workloads, not VM resilience.                                                                                                                               |
| Current Proxmox CSI         | Maybe for Proxmox-pinned VMs         | Could preserve disks across some Kubernetes node churn, but depends on Proxmox topology/API access and does not satisfy OVH-only resilience goals.                                               |
| Longhorn                    | Good candidate if revived            | Distributed block storage, snapshots, backups, replica rebuild. Harvester is built around it. We previously retired it for active workloads, so this needs a fresh trial rather than assumption. |
| Rook/Ceph                   | Strong conventional KubeVirt storage | Most standard serious self-hosted answer. Heavier operational cost and memory/disk overhead. Could provide RBD snapshots and CephFS/RWX paths depending on design.                               |
| NFS/NAS                     | Simple RWX, mixed reliability/perf   | Easy live-migration substrate if backed by reliable NAS, but one NAS can become the new single point of failure.                                                                                 |
| SeaweedFS CSI/FUSE          | Not recommended for VM disks         | Existing repo notes already treat it as experimental/POSIX-ish and unsuitable for core infra until more validation.                                                                              |

## Recommendation

Adopt upstream KubeVirt first, not Harvester, if the goal is to add VMs to the
existing cluster.

Initial scope should be explicit:

- KubeVirt is for persistent Linux utility VMs and maybe legacy software.
- First storage target is disposable/local, so no HA claims.
- HA/live migration is a phase-two storage project.
- VM snapshots require adding and validating a `VolumeSnapshotClass`.
- "Suspend/resume" means either KubeVirt pause/unpause while keeping RAM on the
  same node, or normal guest shutdown/start. Do not promise durable
  suspend-to-disk unless we select a platform that explicitly exposes that.

If the real requirement is "VMs should feel like a small VMware/Proxmox cluster
with UI, storage, backup, snapshots, and live migration," evaluate Harvester/SUSE
Virtualization as a separate HCI layer. That is more existing solution and less
DIY than assembling upstream KubeVirt plus storage plus UI ourselves.

If the real requirement is "ephemeral fast sandboxes that fork from warmed
memory snapshots," raw Firecracker or Cloud Hypervisor is the correct primitive,
but the existing Kubernetes ecosystem still mostly makes that a custom
orchestrator project.

## Suggested Next Experiment

Run a bounded KubeVirt spike:

1. Pick one KVM-capable worker node.
2. Install KubeVirt and CDI with Flux or a temporary manifest.
3. Create one Fedora/Ubuntu VM with cloud-init and no important data.
4. Validate: start, stop, console, SSH, reboot, pod cleanup.
5. Validate storage behavior on `lvm-proxmox-ssd` or another disposable class.
6. Separately choose a snapshot-capable storage backend before testing
   `VirtualMachineSnapshot`.
7. If that works, evaluate one HA storage option: Longhorn revival vs Rook/Ceph.

Stop the spike if KubeVirt needs host-level changes that are awkward on Talos,
or if storage turns into a bigger project than the VM use case justifies.

## Sources

- KubeVirt installation requirements:
  <https://kubevirt.io/user-guide/cluster_admin/installation/>
- KubeVirt lifecycle and pause semantics:
  <https://kubevirt.io/user-guide/user_workloads/lifecycle/>
- KubeVirt live migration:
  <https://kubevirt.io/user-guide/compute/live_migration/>
- KubeVirt snapshot/restore API:
  <https://kubevirt.io/user-guide/storage/snapshot_restore_api/>
- KubeVirt memory dump note:
  <https://kubevirt.io/user-guide/compute/memory_dump/>
- KubeVirt unresponsive nodes:
  <https://kubevirt.io/user-guide/cluster_admin/unresponsive_nodes/>
- Talos KubeVirt guide:
  <https://www.talos.dev/v1.9/advanced/install-kubevirt/>
- Harvester overview:
  <https://docs.harvesterhci.io/v1.7/>
- Harvester third-party CSI support:
  <https://docs.harvesterhci.io/v1.5/advanced/csidriver/>
- Longhorn storage concepts:
  <https://longhorn.io/docs/latest/concepts/>
- Virtink:
  <https://github.com/smartxworks/virtink>
- Kata Containers virtualization design:
  <https://github.com/kata-containers/kata-containers/blob/main/docs/design/virtualization.md>
- Kata Containers limitations:
  <https://github.com/kata-containers/kata-containers/blob/main/docs/Limitations.md>
- Cloud Hypervisor snapshot/restore:
  <https://intelkevinputnam.github.io/cloud-hypervisor-docs-HTML/docs/snapshot_restore.html>
- Cloud Hypervisor live migration:
  <https://intelkevinputnam.github.io/cloud-hypervisor-docs-HTML/docs/live_migration.html>
- Firecracker snapshotting:
  <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>
