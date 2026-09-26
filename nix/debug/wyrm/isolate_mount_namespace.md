# wyrm2: Reducing `gvfs-udisks2-volume-monitor` CPU burn from k8s mount churn

Date: 2026-03-31

## Problem

`gvfs-udisks2-volume-monitor` burns ~12% CPU on wyrm2 (NixOS desktop + k8s
worker). GVFS polls `/proc/self/mountinfo` on every mount event. A k8s worker
with ~85 pods generates ~626 mount entries (193 overlay, 223 tmpfs, 83 nsfs),
causing constant churn.

## Baseline (pre-change)

| Metric                             | Value |
| ---------------------------------- | ----- |
| Total `/proc/self/mountinfo` lines | 626   |
| `overlay` mounts                   | 193   |
| `tmpfs` mounts                     | 223   |
| `nsfs` mounts                      | 83    |
| `gvfs-udisks2-volume-monitor` CPU  | 11.9% |

## Attempt 1: `MountFlags=slave` on containerd (FAILED)

**Approach**: Set `MountFlags=slave` on `containerd.service` and
`JoinsNamespacesOf=containerd.service` on `kubelet.service`. This runs
containerd in a slave mount namespace so its overlay/snapshot/shm/nsfs mounts
don't appear in the host's mountinfo.

**Result**: Broke Cilium completely. All pod creation hung.

**Root cause**: `MountFlags=slave` makes the _entire_ mount namespace slave,
including `/sys/fs/bpf`. Cilium agent requires `/sys/fs/bpf` to be a shared
mount for its BPF filesystem. Error:

```
path "/sys/fs/bpf" is mounted on "/sys/fs/bpf" but it is not a shared mount
```

The CNI socket (`/var/run/cilium/cilium.sock`) also never appeared on the host,
so all pod sandbox creation failed waiting for Cilium.

**Reverted** by removing the option and rebooting.

## Attempt 2: Targeted `--make-private` on containerd dirs (FAILED)

**Approach**: Instead of slaving the whole namespace, bind-mount
`/var/lib/containerd` and `/run/containerd` onto themselves and mark them
`private` via `ExecStartPre`. The idea was that submounts under private mount
points wouldn't propagate to the host mountinfo.

**Result**: No effect. Overlay mounts still visible, gvfs still at ~8% CPU.

**Root cause**: Mount propagation flags (`shared`/`private`/`slave`) control
propagation **between mount namespaces**, not visibility **within** a namespace.
Every mount in a namespace is always visible in that namespace's
`/proc/self/mountinfo` regardless of propagation flags. The `--make-private`
only prevents cross-namespace propagation, which is irrelevant when gvfs runs
in the same namespace as containerd.

Additionally, the overlay mounts inherit propagation from their parent mount
(`/run` tmpfs), not from our bind-mounted `/run/containerd`. The bind mount
stacks on top but doesn't change the parent that containerd's overlays attach
to.

## Key insight

The only way to hide mounts from a process's `/proc/self/mountinfo` is to put
either that process or the mount-creating process in a **different mount
namespace**. Within a single namespace, all mounts are always visible.

This means:

- Any containerd-side isolation (`MountFlags`, `--make-private`) will break
  things that need bidirectional mount propagation (Cilium BPF, CSI volumes)
- The fix must be on the gvfs side

## Future options

### 1. Mask `gvfs-udisks2-volume-monitor` (simplest)

```bash
systemctl --user mask gvfs-udisks2-volume-monitor
```

Or via NixOS, override the systemd user unit to be masked. Zero risk. Only
cost: no auto-mount of USB/external drives in Nautilus file manager. Can use
`udisksctl mount` manually.

### 2. NixOS systemd user unit override with `PrivateMounts=yes`

Run `gvfs-udisks2-volume-monitor` in its own mount namespace so it doesn't see
container mounts. This would keep USB auto-mount working while hiding the k8s
mount churn. Needs testing — GVFS might need host mount visibility to function.

### 3. Upstream fix in GVFS

GVFS could filter `/proc/self/mountinfo` by path prefix (ignore
`/run/containerd/*`, `/var/lib/containerd/*`). No such filter exists today.
Unlikely to land upstream since this is a niche use case (desktop + k8s worker
on same machine).
