# VM Orchestration Alternatives

Comparison of systems that can run VMs or sandboxed workloads on k8s,
focused on **snapshot/restore** and **fork-resume** (restoring one snapshot
into multiple independent instances).

## Requirements

1. **Save full state to disk**: memory + CPU registers + device state
2. **Restore from disk**: resume a saved VM without cold-booting
3. **Fork-resume**: one snapshot → N resumed instances (each diverges independently)
4. **Fast restore**: target <100ms for the VMM restore operation
5. **k8s integration**: manageable from k8s (CRD, RuntimeClass, or API)

## Comparison

| System                   | Save to disk                     | Restore               | Fork-resume                               | Restore latency                | k8s integration                  |
| ------------------------ | -------------------------------- | --------------------- | ----------------------------------------- | ------------------------------ | -------------------------------- |
| Firecracker (standalone) | Yes (vmstate + memory file)      | Yes                   | Yes, first-class (COW mmap)               | ~5–10ms VMM + demand-paged mem | None (need Kata or custom)       |
| Kata + Firecracker       | **No** (FC APIs not wired up)    | Boot template only    | Boot template only (COW)                  | ~100–200ms                     | High (shimv2 CRI)                |
| Kata + QEMU              | Boot template only (`-incoming`) | Boot template only    | Boot template only (COW)                  | ~100–200ms                     | Highest (default Kata config)    |
| Cloud Hypervisor         | Yes (snapshot API)               | Yes                   | Yes (manual, no built-in COW)             | ~50–200ms                      | Moderate (via Kata)              |
| QEMU/microvm             | Yes (migration/savevm)           | Yes                   | Yes (via `-incoming`)                     | ~100–500ms                     | Low (experimental Kata)          |
| KubeVirt                 | Disk only (CSI VolumeSnapshot)   | Disk only (cold boot) | Disk clone only (VirtualMachineClone CRD) | Cold boot (seconds)            | High (CRD/operator)              |
| gVisor                   | Yes (runsc checkpoint)           | Yes                   | Partial (not first-class)                 | ~50–200ms                      | High (runtime), low (checkpoint) |

## Detailed Notes

### Firecracker (standalone)

The gold standard for fork-resume. Used at AWS Lambda to snapshot a warmed
function and restore it across thousands of workers. Snapshot = vmstate file

- memory file. The memory file can be mmap'd COW so clones share base pages
  and only allocate for dirty pages. Restore is ~5–10ms for the VMM operation;
  memory is demand-paged from the file.

**Diff snapshots** are supported: after restoring, enable dirty-page tracking
and take a diff snapshot that only contains pages modified since restore.

No k8s integration — needs a controller (what we're building) or Kata.

### Kata Containers

Kata has two acceleration mechanisms, both in the `[factory]` section of
`configuration.toml`. Neither exposes arbitrary-point snapshots.

#### VM Templating (`enable_template = true`)

**QEMU-only.** Uses QMP migration, not Firecracker snapshots.

Step by step:

1. Boot a VM with kernel + initrd + kata-agent
2. Pause via QMP once agent is ready
3. Save memory + device state to `template_path` (default `/run/vc/vm/template`)
   via QEMU's incoming migration mechanism
4. New sandboxes restore from the template with COW memory mapping — shared
   read-only base pages, private dirty pages per VM

Results: ~73% reduction in startup latency, ~80% reduction in memory per
container (shared kernel/agent pages). In a 100-container test with 128MB
guests: ~9GB total memory saved.

Constraints:

- Requires `initrd=` (not `image=`)
- Must NOT use `shared_fs = "virtio-fs"` (incompatible)
- QEMU >= v4.1.0
- Security: shared read-only memory mapping is vulnerable to cross-VM
  side-channel attacks (CVE-2015-2877). Not for multi-tenant.

**Key limitation: only captures post-boot, pre-workload state.** You cannot
snapshot a pod after your application has been running. The template is the
kernel+agent boot state, not your warmed-up Bazel JVM.

#### VM Cache (`vm_cache_number > 0`)

Pre-creates a pool of booted VMs held ready via a gRPC server on
`vm_cache_endpoint` (default `/var/run/kata-containers/cache.sock`).
New sandboxes grab a pre-warmed VM from the pool instead of booting.

Reportedly broken in Kata 2.x+ (issue #1106), limited maintenance.

#### `configuration.toml` keys

| Key                 | Default                               | Description                    |
| ------------------- | ------------------------------------- | ------------------------------ |
| `enable_template`   | `false`                               | Clone from pre-booted template |
| `template_path`     | `/run/vc/vm/template`                 | Where template state is saved  |
| `vm_cache_number`   | `0`                                   | Pre-warmed VM pool size        |
| `vm_cache_endpoint` | `/var/run/kata-containers/cache.sock` | gRPC socket for cache          |

#### CLI

```bash
kata-runtime factory init      # create template (or: auto-created on first container)
kata-runtime factory destroy   # destroy template
kata-ctl factory ...           # same, Rust runtime (v3.23.0+)
```

No commands for snapshotting running sandboxes, listing snapshots, or restoring.

#### Kata + Firecracker: no snapshot passthrough

Despite Firecracker having `PUT /snapshot/create` and `PUT /snapshot/load`
APIs, **Kata does not wire them up.** The `configuration-fc.toml` has a
`[factory]` section with `enable_template` but the underlying implementation
uses QEMU QMP migration, which Firecracker does not support. Kata's
Limitations.md explicitly states: "The runtime does not provide checkpoint
and restore commands."

#### Per-pod configuration

Different pods can use different Kata configs via separate `RuntimeClass`
objects (each pointing to a different `configuration.toml`). But there is no
per-pod annotation to override `enable_template` or `template_path`. Template
config is per-RuntimeClass, not per-pod.

#### Recent developments (2024–2025)

- **runtime-rs VM templating** (v3.23.0): The Rust runtime gained VM
  templating support (PR #11828). Same QEMU QMP mechanism as the Go runtime.
  ~73% startup latency reduction, ~80% memory savings.
- **Koyeb's custom fork**: Koyeb patched the Kata shim to add
  `pause_with_snapshot` / `resume_from_snapshot` for Cloud Hypervisor,
  achieving ~200ms wake times for scale-to-zero. **Custom fork, not upstream.**
- **No upstream checkpoint/restore RFC**: No design document for full VM
  state snapshots exists in upstream Kata.

#### What Kata cannot do that raw Firecracker can

| Capability                           | Raw Firecracker | Kata                                |
| ------------------------------------ | --------------- | ----------------------------------- |
| Snapshot running VM with workload    | Yes             | **No**                              |
| Restore from arbitrary snapshot      | Yes             | **No** (boot template only)         |
| Fork-resume (snapshot → N instances) | Yes (COW mmap)  | Boot template only                  |
| Firecracker snapshot passthrough     | N/A             | **No** (APIs not wired up)          |
| Per-pod snapshot config              | N/A             | **No** (per-RuntimeClass)           |
| Checkpoint/restore (CRIU-like)       | N/A             | **No** (excluded in Limitations.md) |

### Cloud Hypervisor

Intel-originated VMM (now Linux Foundation). Cleaner codebase than QEMU,
more features than Firecracker (PCI, VFIO, vhost-user, hotplug). Snapshot
API (`PUT /vm.snapshot`, `PUT /vm.restore`) saves to a directory.

Fork-resume works (restore the same snapshot directory N times) but there's
no built-in COW memory sharing — each restore loads its own copy. You'd need
filesystem-level COW (btrfs/XFS reflinks) to share base pages across clones.

#### k8s integration

Cloud Hypervisor runs on k8s via **Kata Containers** (`kata-clh` runtime
class). But Kata doesn't expose CH's snapshot APIs either — same limitation
as with Firecracker. Kata only uses CH for boot-time VM templating.

For arbitrary-point snapshots + fork-resume, you'd need a custom controller
(same as with Firecracker). The workflow would be structurally identical:

#### Workflow comparison: Cloud Hypervisor vs Firecracker

**Firecracker (what we have):**

1. VM pod starts `firecracker --api-sock /tmp/fc.sock`
2. Configure via REST: `PUT /boot-source`, `PUT /drives/rootfs`, etc.
3. Snapshot: `PUT /snapshot/create {"snapshot_type": "Full", ...}`
   → vmstate file + memory file
4. Fork-resume: new pod starts `firecracker --api-sock ...`,
   then `PUT /snapshot/load` with memory file mmap'd COW
5. Diff snapshots: enable dirty-page tracking, take incremental snapshot

**Cloud Hypervisor (hypothetical):**

1. VM pod starts `cloud-hypervisor --api-socket /tmp/ch.sock`
   (or via `ch-remote` CLI for all API calls)
2. Configure via REST or CLI:
   `PUT /vm.create {"payload": {"kernel": ..., "initramfs": ...}, ...}`
   `PUT /vm.boot`
3. Snapshot: `PUT /vm.snapshot {"destination_url": "/data/snapshots/warm"}`
   → directory with config.json + memory region files + device state
4. Fork-resume: new pod starts
   `cloud-hypervisor --api-socket ... --restore "source_url=/data/snapshots/warm"`
   Each restore loads its own copy of memory (no built-in COW mmap).
   For COW sharing: store snapshots on XFS/btrfs, use reflinks when
   copying to each pod's working directory.
5. No diff snapshots — each snapshot is a full dump.

#### Tradeoffs vs Firecracker

| Feature                  | Firecracker               | Cloud Hypervisor             |
| ------------------------ | ------------------------- | ---------------------------- |
| Fork-resume COW          | Built-in (mmap mem file)  | Manual (fs reflinks or copy) |
| Restore latency          | ~5–10ms VMM               | ~50–200ms                    |
| Diff snapshots           | Yes (dirty-page tracking) | No                           |
| virtio-fs                | No                        | Yes                          |
| PCI / VFIO passthrough   | No                        | Yes                          |
| Hotplug (CPU, mem, disk) | No                        | Yes                          |
| Live migration           | No                        | Yes                          |
| Binary size              | ~3MB                      | ~10MB                        |
| Device model             | Minimal (virtio-mmio)     | Rich (PCI, ACPI, IOAPIC)     |

**When CH would be better**: if we needed virtio-fs (sharing host dirs into
guest without baking into rootfs), GPU/VFIO passthrough, or live migration
between hosts. None of these are currently needed.

**When Firecracker is better**: fork-resume speed and memory efficiency are
the primary differentiators. 5–10ms vs 50–200ms restore, plus built-in COW
memory sharing means N forked VMs from the same snapshot share base pages
automatically. With CH you'd need to orchestrate reflinks yourself and each
VM still pays the full restore cost.

### KubeVirt

The only system with a full k8s-native CRD experience (VirtualMachine,
VirtualMachineSnapshot, VirtualMachineClone). But **snapshots are disk-only**
via CSI VolumeSnapshot. There is no memory state capture. Restore means
cold-booting from the snapshotted disk. VirtualMachineClone creates a new VM
from a snapshot with deduplicated identity (new MAC, UUID).

KubeVirt uses QEMU/libvirt underneath, which _does_ support `virsh save`
(memory to file), but KubeVirt's orchestration layer has not exposed this.
Pause/unpause keeps RAM allocated (not saved to disk).

Not suitable for our fork-resume use case.

### gVisor

Not a VM — a user-space kernel that intercepts syscalls. `runsc checkpoint`
saves full process state (memory, FDs, network, kernel state) to a file.
Restore via `runsc restore`. Used by Google Cloud Run for instance pre-warming.

Fork-resume is technically possible (restore the same checkpoint N times) but
is not a promoted use case. May have issues with duplicate network state.
Checkpoint/restore is not exposed via k8s APIs (separate from the k8s
Forensic Container Checkpointing KEP which uses CRIU).

## Assessment for Our Use Case

We want to: boot a NixOS dev VM, warm up Bazel (JVM + Skyframe cache),
snapshot, then fork-resume into multiple sessions that each start from the
warm state.

**Best fit: Firecracker standalone with a custom controller** (what we have).
Firecracker's snapshot/restore is purpose-built for this, with the fastest
restore (~5ms VMM + demand-paged memory) and first-class COW fork-resume.
The tradeoff is building our own orchestration.

**Runner-up: Cloud Hypervisor** has full snapshot/restore and fork-resume,
with a richer device model than Firecracker. No built-in COW sharing, but
filesystem-level reflinks could substitute. Available as a Kata backend.

**Not suitable: Kata** (only exposes boot-time templating, no arbitrary-point
snapshots, Firecracker snapshot APIs not wired up), **KubeVirt** (no memory
snapshots), **gVisor** (fork-resume not first-class).
