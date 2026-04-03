# Firecracker Dev VMs — Design Document

Warm Firecracker microVMs on the home cluster for Claude Code development.

## Problem

Claude Code web sessions run inside Anthropic's Firecracker VMs with:

- ~41G usable disk (256G ext4 with 84% reserved blocks — fixable via `tune2fs`)
- Bazel cold start: 11-29s (JVM startup + module extension eval + BCR fetches)
- First warm Bazel query: ~6s (Starlark eval: pip 3.4s, npm 2.2s, go_sdk 1.1s)
- Subsequent warm queries: ~0.3s (filesystem diff scanning)
- Session start overhead: ~13s (proxy setup, Bazel warmup, env config)
- Disk fills up from accumulated session Bazel caches (2-12G each)

Builds execute remotely via BuildBuddy RBE — the local VM is effectively
just an expensive JVM host. Profiling data: <devinfra/precommit/enforce_bazel_tests/debug/>.

## Goal

Run Firecracker microVMs on wyrm2 (bare metal NixOS, 32 CPU, 94G RAM, KVM,
2x RTX 5090) that Claude Code sessions can SSH into. VMs have:

- Internet access (for git clone, BCR fetches, BuildBuddy RBE)
- Bazel + Python 3.13 + full build toolchain
- Fast startup via snapshot/restore (~28ms vs ~15s cold boot)
- Persistent caches across sessions

## Non-Goals

- Replacing Anthropic's Firecracker VMs (those handle Claude Code itself)
- Running untrusted code (VMs run our own toolchain, not user code)
- Multi-tenant isolation (single user, trusted workloads)

## Requirements

### Functional

1. Claude Code session can create a VM via authenticated API call
2. Claude Code session can SSH into the VM and run commands
3. VM has internet access (git, curl, Bazel BCR, BuildBuddy)
4. VM has Bazel, Python 3.13, JDK 21, Git, build-essential
5. VM rootfs is reproducible (Nix-built)
6. Multiple VMs can run concurrently (separate pods)
7. VMs can be snapshotted and restored with warm Bazel state

### Non-Functional

1. VM creation: <60s cold, <5s from snapshot
2. SSH latency: <50ms within cluster
3. No full privileged pods — only `/dev/kvm` + `NET_ADMIN`
4. GitOps-managed (Flux) for all cluster resources
5. CI-published images (rootfs, manager, VM pod)

### Acceptance Criteria

- [ ] Device plugin exposes `/dev/kvm` on wyrm2
- [ ] `nix build .#fc-dev-rootfs` produces bootable ext4 + vmlinux
- [ ] Initramfs with process_api boots, pivot_roots to NixOS on /dev/vda
- [ ] API: `POST /vms` creates a VM pod, returns WebSocket endpoint
- [ ] WebSocket `CreateProcess` runs commands, streams I/O
- [ ] `curl ifconfig.me` inside VM returns an IP (guest networking works)
- [ ] `bazel info` inside VM succeeds (JVM starts, finds workspace)
- [ ] `bazel test //util/bazel/...` passes inside VM (RBE works)
- [ ] Snapshot: warm Bazel, snapshot, restore — query takes ~0.3s not ~15s
- [ ] All k8s resources reconcile via Flux without manual intervention

## Architecture

### Storage: LVM thin provisioning via OpenEBS LVM LocalPV

All VM storage lives on a single LVM thin pool on wyrm2. OpenEBS LVM
LocalPV is a lightweight CSI driver (single DaemonSet) that wraps LVM
commands. Two volume modes from the same VG:

| Resource     | volumeMode   | Provisioning             | Why                                                                                                                                                               |
| ------------ | ------------ | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Rootfs**   | `Block`      | Thin snapshot of base LV | Firecracker drives API accepts block devices. Instant CoW clone. No filesystem overhead.                                                                          |
| **Snapshot** | `Filesystem` | Thin-provisioned, ext4   | Firecracker's snapshot code uses `ftruncate()` + `metadata().len()` — requires regular files (see below). One PVC per snapshot holds both `memory` and `vmstate`. |

#### Why rootfs is Block but snapshots are Filesystem

Firecracker's drive API (`PUT /drives/{id}`) accepts `path_on_host` as
either a regular file or a block device — both work.

Firecracker's snapshot API does **not** work with block devices:

- **Create** (`PUT /snapshot/create`): calls `ftruncate()` to size the
  memory file → `EINVAL` on block devices.
- **Load** (`PUT /snapshot/load`): calls `file.metadata().len()` which
  returns 0 for block devices → fails size check before reaching `mmap`.

The actual I/O (write, mmap MAP_PRIVATE) would work on block devices —
it's only the Rust file metadata + truncation assumptions that break.
(Source: `firecracker/src/vmm/src/vstate/vm.rs` snapshot_memory_to_file,
`src/vmm/src/vstate/memory.rs` snapshot_file.)

#### PVC lifecycle

Each VM gets two PVCs:

- **Rootfs** (`Block`): thin clone of base
- **Work** (`Filesystem`): thin-provisioned, holds snapshot files at `/work/snapshots/`

```
POST /vms → manager creates:
  1. Rootfs PVC (Block, dataSource: base) → instant CoW
  2. Work PVC (Filesystem, thin) → mounted at /work
  3. Pod with rootfs as /dev/rootfs, work at /work

POST /vms/{id}/snapshot {name} →
  4. Firecracker writes memory + vmstate to /work/snapshots/{name}/
     (files persist on the VM's work PVC)

POST /snapshots/{name}/restore →
  5. New rootfs PVC (clone of source VM's rootfs)
  6. New snapshot PVC (clone of source VM's work PVC) → CoW, not shared
  7. New work PVC (fresh, for the restored VM's own use)
  8. New pod: rootfs as /dev/rootfs, snapshot at /snapshots, work at /work

DELETE /vms/{id} → deletes pod + all per-VM PVCs
```

#### Fork-resume (one snapshot → N VMs)

Each restored VM gets its own CoW clone of the snapshot PVC — no PVC
sharing between VMs. LVM thin snapshots are instant and CoW at the
block level, so N restores from one snapshot create N thin LVs that
share physical extents until written.

Inside the guest, Firecracker mmaps the memory file with `MAP_PRIVATE`
(CoW at the page level). Each VM gets its own dirty pages while sharing
the base memory via the LVM thin snapshot underneath.

#### Base rootfs provisioning

The NixOS rootfs is built via Nix on wyrm2 (which has the repo and Nix
available). No OCI wrapping — just `nix build` + `dd` to the base LV:

```bash
./devinfra/firecracker/provision-rootfs.sh
```

The script builds `.#fc-dev-rootfs` (fetches from binary cache if
available), finds the ext4 image in the output, and `dd`s it into the
base LV. Re-run when Nix config changes. The rootfs updates infrequently
(kernel, NixOS modules, toolchain packages).

#### Setup

1. Attach a block device to wyrm2 (Proxmox virtual disk, ~100GB)
2. Create VG + thin pool (OpenEBS LVM LocalPV is deployed, VG: `openebs-lvmvg`)
3. Create block-mode StorageClass `lvm-proxmox-block` (same VG, no fstype)
4. Create base rootfs LV and run `provision-rootfs.sh`

### Guest init: process_api via initramfs

Anthropic's `process_api` is a ~3.3MB static Rust binary that serves as
PID 1 in Claude Code web Firecracker VMs. Full reverse-engineered source
(5,752 lines, 10 modules) at <devinfra/claude/web_env/re/process_api/>.

We use it as our guest init, following Anthropic's architecture:

```
VM pod entrypoint (host side)
  │
  │ Firecracker API:
  │   PUT /boot-source  {kernel, initrd_path: initramfs.cpio}
  │   PUT /drives/rootfs {path_on_host: nixos-rootfs.ext4}  → /dev/vda
  │   PUT /actions       {InstanceStart}
  │
  ▼
┌─ Firecracker guest ────────────────────────────────┐
│                                                     │
│  Kernel boots → unpacks initramfs to tmpfs          │
│  Runs /init (process_api --firecracker-init)        │
│                                                     │
│  process_api:                                       │
│    1. mount /proc, /sys, /dev, /dev/pts, cgroup2    │
│    2. configure networking (IP, gateway, DNS)       │
│    3. mount /dev/vda (ext4) → /mnt                  │
│    4. pivot_root /mnt /mnt/oldroot                  │
│    5. umount /oldroot (initramfs freed)             │
│    6. start WebSocket listener (vsock or TCP)       │
│    7. start HTTP control server                     │
│                                                     │
│  Guest is now running NixOS from /dev/vda with      │
│  process_api as PID 1 managing processes via WS.    │
│                                                     │
│  /init in /proc/1/exe → points to initramfs path    │
│  (filesystem gone after pivot_root — invisible)     │
└─────────────────────────────────────────────────────┘
```

**Two filesystem images:**

1. **Initramfs** (~5MB cpio): contains just `process_api` as `/init`.
   Built as a cpio archive, baked into the VM pod OCI image. The kernel
   unpacks it into tmpfs at boot. After pivot_root the tmpfs is freed.

2. **NixOS rootfs** (~2-4GB ext4): full dev environment. Exposed to
   guest as `/dev/vda` via Firecracker's drive API. Delivery mechanism
   is an open decision (see Storage section).

**What process_api provides:**

- **Process execution over WebSocket**: spawn processes, forward I/O as
  binary frames, reattachable sessions that survive disconnects
- **vsock + TCP + UDS transports**: same protocol over any transport
- **HTTP control server**: `/health`, `/shutdown`, `/fs_freeze`,
  `/fs_thaw`, `/mount_root` (snapstart — apply per-session config on
  snapshot restore)
- **Cgroup v1/v2 resource limits**: per-process memory/OOM monitoring
- **JWT auth**: Ed25519-verified tokens (optional — accepts all if no key)

**Trade-off**: proprietary binary we can't rebuild. If a Firecracker or
kernel update breaks compatibility, fallback is reimplementing the subset
we need (vsock readiness + command exec + fs_freeze/thaw) as a custom
agent. The RE source serves as the spec.

**SSH remains available** in the NixOS rootfs for interactive access from
Claude Code sessions. process_api handles the machine-to-machine control
plane (readiness, process exec, snapshot coordination).

### Pod-per-VM

Each Firecracker VM runs as its own k8s pod. The pod is infrastructure-only
(Firecracker process + networking + port proxies). The manager service is
the VMM brain — it creates pods, drives boot/restore via the FC API proxy,
and tracks VM state.

```
Claude Code session (Anthropic Firecracker VM)
  │
  │ 1. POST /vms → manager creates PVC (cloned from base) + pod
  │ 2. POST /vms/{id}/boot → manager configures FC via API proxy
  │ 3. Manager returns pod IP + ports
  │ 4. Claude Code connects to process_api WS on pod_ip:2024
  │
  ├──── Manager API ──────────────────────────────────┐
  │     POST /vms, POST /vms/{id}/boot                │
  │     POST /vms/{id}/snapshot, POST /vms/{id}/restore│
  │     DELETE /vms/{id}, GET /vms                     │
  └───────────────────────────────────────────────────┘
  │
  │ Manager drives boot/restore via FC API proxy (:2026)
  │ Manager waits for guest /health via control proxy (:2025)
  │ Claude Code connects to WS proxy (:2024)
  ▼
┌─ VM Pod (infrastructure only) ──────────────────────┐
│ entrypoint:                                          │
│   1. Create TAP + NAT (pyroute2 + nft)              │
│   2. Start Firecracker process (no VM config)       │
│   3. Proxy ports onto pod network:                  │
│        :2024 → guest:2024 (process_api WS)          │
│        :2025 → guest:2025 (process_api HTTP)        │
│        :2026 → FC Unix socket (management API)      │
│   4. Block until Firecracker exits                  │
│                                                      │
│ ┌─ Firecracker VM ─────────────────────────────────┐│
│ │ process_api (PID 1, from initramfs)              ││
│ │   ├─ WebSocket :2024 — process execution         ││
│ │   ├─ HTTP :2025 — /health, /shutdown, /fs_freeze ││
│ │   └─ pivot_root to NixOS on /dev/vda             ││
│ │                                                   ││
│ │ NixOS guest (sshd for interactive access)        ││
│ │ Bazel + Python 3.13 + JDK 21 + Git              ││
│ │ eth0 → TAP → pod eth0 → internet                ││
│ └───────────────────────────────────────────────────┘│
│                                                      │
│ /dev/kvm (via device plugin)                         │
│ NET_ADMIN (for TAP + NAT)                            │
│ PVC: rootfs (cloned from base by manager)            │
│ emptyDir: /work (snapshots, runtime)                 │
└──────────────────────────────────────────────────────┘
```

### Command execution

Claude Code drives the VM via **process_api's WebSocket API** (port 2024),
the same protocol Anthropic uses in their own Claude Code web VMs:

1. Manager creates VM pod, waits for process_api `/health` to respond
2. Manager returns WebSocket connection info (pod IP + port) to caller
3. Claude Code connects to process_api WebSocket directly
4. Sends `CreateProcess` with command, args, env, uid/gid, timeout
5. process_api spawns the process, streams stdout/stderr as binary frames
6. Client sends stdin, signals (SIGINT, SIGTERM)
7. Sessions are **reattachable** — if the WebSocket disconnects, the
   process keeps running. Client reconnects with `ProcessConnection`.

SSH remains available in the NixOS guest for interactive/ad-hoc access,
but the primary machine-to-machine interface is the WebSocket API.

### Networking

Each VM pod gets a CNI-assigned IP. Inside the pod, the entrypoint:

1. Creates a TAP device
2. NATs via nftables MASQUERADE (pyroute2 for TAP, `nft` for NAT)
3. Firecracker attaches the TAP as a virtio-net device
4. Guest gets an IP on the TAP subnet, routes through pod's `eth0`
5. process_api listens on TCP :2024 (WebSocket) and :2025 (HTTP control)

Claude Code sessions reach VMs via `kubectl port-forward` to the VM pod
(port 2024 for WebSocket, port 22 for SSH).

### Authentication

Manager service uses bearer token auth. Token stored as k8s Secret in
`claude-sandbox`, available to Claude Code sessions via the existing
kubeconfig (ServiceAccount `claude-code-web` has Secret read access).

process_api supports optional JWT auth (Ed25519). With no key configured
it accepts all tokens — sufficient for our single-user trusted setup.
Can be hardened later by injecting an auth key via the manager config.

### Snapshot/Restore

Firecracker's snapshot API (`PUT /snapshot/create`, `PUT /snapshot/load`)
captures full VM state (memory + vCPU registers + device state). Restore
uses `userfaultfd` lazy memory loading — pages are demand-faulted from the
snapshot file, achieving ~28ms restore time.

Workflow:

1. Boot VM, clone repo, `bazel query 'tests(//...)'` (warms Skyframe)
2. Pause VM: `PUT /vm` with `state: Paused`
3. Snapshot: `PUT /snapshot/create` → memory + state files on PVC
4. Restore: new pod starts Firecracker with `--restore-from-snapshot`
5. Fixups: re-seed entropy, reconnect SSH, `git pull`

Post-restore, Bazel queries take ~0.3s (warm Skyframe) instead of ~15s.

### Gotchas on Snapshot Restore

| Issue             | Cause                                 | Fix                              |
| ----------------- | ------------------------------------- | -------------------------------- |
| Entropy           | RNG state identical across clones     | Re-seed via virtio-rng or vsock  |
| Clock skew        | Guest clock frozen at snapshot time   | `ntpdate` or inject via serial   |
| Stale connections | TCP connections dead post-restore     | Reconnect (sshd restarts accept) |
| Git state         | Repo at snapshot-time commit          | `git fetch && git checkout`      |
| Unique identity   | MAC, instance ID shared across clones | Re-randomize on restore          |
| Filesystem        | Rootfs must match snapshot state      | CoW clone from snapshot's rootfs |

## Prior Art

### Anthropic's Snapstart

Anthropic's own `process_api` (reverse-engineered in
`devinfra/claude/web_env/re/process_api/`) implements this exact pattern:

- **Template mode**: Boot VM, run full init, write `"SNAPSTART_READY\n"` to
  serial port. Host snapshots the frozen VM.
- **Resume mode**: Restore snapshot, thaw filesystem (FITHAW ioctl), call
  `POST /mount_root` to apply per-session config (mounts, env vars).
- Communication via vsock + Unix domain sockets.

This validates the architecture — Anthropic uses warm VM pools in production
for Claude Code web.

### ForgeVM

Go binary orchestrating Firecracker sandboxes with 28ms snapshot restore.
Built for AI agent code execution. Uses vsock + custom guest agent (author
regrets not using gRPC). Memory-efficient: 50 VMs from one snapshot share
most pages via CoW. Not k8s-native, no published GitHub repo yet.

Source: [DEV Community writeup](https://dev.to/adwitiya/how-i-built-sandboxes-that-boot-in-28ms-using-firecracker-snapshots-i0k)

### vHive

Research framework (Edinburgh/NTU) for serverless experimentation.
Most mature open-source Firecracker snapshot implementation on k8s
(via Knative). Supports their REAP mechanism — records guest memory
working set and proactively prefetches on restore (reduces page fault
overhead by 95%). Research-grade, not production infra. Requires owning
the entire k8s cluster.

Source: [github.com/vhive-serverless/vHive](https://github.com/vhive-serverless/vHive)

### Kata Containers

Production k8s runtime supporting Firecracker as a VMM backend. But
snapshot/restore within Kata+Firecracker is not first-class — the
VM cache feature works mainly with QEMU/Cloud Hypervisor. The
Kata+Firecracker stack is reported as difficult to configure (devmapper
snapshotter requirements, stale docs).

### Rejected Alternatives

| Project   | Why rejected                                                 |
| --------- | ------------------------------------------------------------ |
| KubeVirt  | Too heavy: virt-handler leaks to 7-12GB, wraps VMs in QEMU   |
| Hocus     | Abandoned, replaced Firecracker with QEMU for dev envs       |
| Flintlock | Archived (Weaveworks shutdown), no snapshot support          |
| Daytona   | Docker-based (not Firecracker), no VM-level snapshot/restore |
| E2B       | SaaS only, data leaves machine                               |

### firecracker-containerd

AWS's containerd integration. Provides the devmapper snapshotter for
exposing container images as block devices to Firecracker VMs. However,
snapshot/restore of VMs is not exposed through containerd's API — you'd
need to call the Firecracker socket directly. In maintenance mode.

We don't need the containerd integration because we're not running OCI
containers inside Firecracker — we're running a NixOS guest directly.

## Key Decisions

| Decision            | Choice                              | Rationale                                            |
| ------------------- | ----------------------------------- | ---------------------------------------------------- |
| Device access       | Generic device plugin               | Lightest option, no KubeVirt overhead                |
| VM isolation        | Pod-per-VM                          | k8s lifecycle, resource accounting, standard tooling |
| Rootfs build        | Nix                                 | Reuses `bazel-dev.nix`, shares with wyrm2 config     |
| Manager             | FastAPI service                     | Orchestrates pods via k8s API, lightweight           |
| Manager image build | Bazel rules_oci                     | Consistent with repo, mypy checked                   |
| Firecracker binary  | Bazel http_file dep                 | Pinned, reproducible, baked into image               |
| Command execution   | process_api WebSocket               | Same protocol as Anthropic's VMs, reattachable       |
| Network             | TAP + NAT in pod                    | Pod gets CNI networking, VM NATs through it          |
| Auth                | Bearer token (manager), JWT (guest) | k8s Secret for manager, optional Ed25519 for WS      |
| Guest init          | process_api via initramfs           | Production-hardened, vsock, snapstart support        |
| Boot                | Kernel + initramfs + /dev/vda drive | Initramfs has process_api; rootfs on /dev/vda        |

### Open decisions

| Decision         | Options                                | See                                    |
| ---------------- | -------------------------------------- | -------------------------------------- |
| Rootfs delivery  | OCI layer / CSI clone / init-container | Storage section                        |
| Snapshot storage | CSI PVCs / local filesystem            | Storage section (snapshot/fork-resume) |

## Security Model

- `/dev/kvm` via device plugin (not full privileged mode)
- `NET_ADMIN` capability for TAP/NAT (scoped, not `CAP_SYS_ADMIN`)
- Firecracker provides hardware isolation (separate kernel per VM)
- SSH keys managed via k8s Secret
- Bearer token auth on manager API
- All images are CI-built from this repo (not third-party)
- VM pods schedule on any node with `/dev/kvm` (via device plugin)

## Resource Budget

wyrm2: 32 CPU, 94G RAM. Current usage: 19% CPU, 19% memory.

| Component        | CPU request | Memory request | Count |
| ---------------- | ----------- | -------------- | ----- |
| Manager pod      | 100m        | 256Mi          | 1     |
| VM pod (default) | 2           | 4Gi            | 1-3   |
| Device plugin    | 50m         | 64Mi           | 1     |
| **Total**        | ~6.2        | ~12.3Gi        | 3-5   |

Fits comfortably within wyrm2's capacity and the claude-sandbox
ResourceQuota (8 CPU, 16Gi, 20 pods).
