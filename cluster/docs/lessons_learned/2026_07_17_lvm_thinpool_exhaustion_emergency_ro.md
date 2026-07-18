# 2026-07-17 — `lvm-proxmox-ssd` thin-pool exhaustion → PVC `emergency_ro`

## Symptom

A vLLM pod on `wyrm2` downloading a ~62 GB model to its `hf-cache` PVC
(`lvm-proxmox-ssd`, openebs LVM thin) started crash-looping with:

```text
OSError: [Errno 30] Read-only file system: '/hf-cache/hub/.../*.lock'
```

The PVC mount showed the ext4 `emergency_ro` flag:

```text
/dev/mapper/openebs--proxmox--ssd-pvc-… on /hf-cache type ext4 (rw,relatime,stripe=16,emergency_ro)
```

wyrm2's kernel log (`sudo dmesg`) had the real story — a **write I/O failure**,
not corruption:

```text
Buffer I/O error on dev dm-21 … lost async page write
EXT4-fs error (dm-21): Detected aborted journal
EXT4-fs (dm-21): Delayed block allocation failed … err 5 … Data will be lost
EXT4-fs (dm-21): Remounting filesystem read-only
```

## Root cause

The **LVM thin pool was 100 % full on physical data extents** while the VG had
plenty of free space:

```text
openebs-proxmox-ssd_thinpool   data 100.00%   meta 46.55%   # the PVC's pool
VG openebs-proxmox-ssd          VSize 500g     VFree 469g    # pool never grew
```

openebs lvm-localpv creates a **dedicated thin pool per StorageClass**, sized to
the PVC, and (on this host) **does not auto-extend** it. When the model download
allocated new blocks past the pool's size, `dm_thin` returned `EIO` → ext4
aborted the journal and remounted read-only. It is **thin-pool exhaustion**, not
a failing disk (`err 5` = EIO on _new block allocation_, with the VG far from
full).

Contributor: earlier `rm` of unused model weights **did not** return blocks to
the pool — thin volumes only reclaim on `fstrim`/discard, and the CSI mount has
no `discard` option. So deleted-but-unreclaimed blocks stayed pinned and the
next big download topped the pool out.

## What did NOT work

- **Detach/reattach** (delete pod → PV remounts fresh) clears `emergency_ro`
  _transiently_, but the next heavy write re-hits the full pool and it flips
  back to read-only.
- **`fstrim` in a pod** to reclaim the deleted blocks: `FITRIM ioctl failed:
Operation not permitted`. It needs `CAP_SYS_ADMIN`; the namespace's `baseline`
  PodSecurity **enforces** no-privileged / no-added-caps, so no in-pod fstrim.

## Fix that worked

**Delete the PVC and recreate it larger.** Deleting the PVC makes openebs remove
the thin volume _and its dedicated pool_, returning everything to the VG. Recreating
`hf-cache` at `400Gi` made openebs provision a **400 GB pool** (VG had the room),
which comfortably holds the 62 GB model with headroom for later ~90 GB GGUFs.

```bash
kubectl -n llm-bench delete pvc hf-cache            # frees the old undersized pool
# recreate PVC at 400Gi on lvm-proxmox-ssd → openebs makes a 400 GB thin pool
```

## Takeaways

- `emergency_ro` on an `lvm-proxmox-*` PVC ⇒ **check the thin pool's `data%`
  first** (`sudo lvs <vg> -o lv_name,lv_size,data_percent`), not SMART/the disk.
- A 100 %-full pool is a **cluster-wide write risk**: every PVC in that pool
  (incl. Proxmox-pinned CNPG DBs) will flip read-only on the next new-block write.
- Size `lvm-proxmox-ssd` PVCs to the pool you actually want — the pool is sized to
  the PVC and won't auto-extend here. Big model caches want a big PVC up front.
- `rm` doesn't shrink a thin volume; only `fstrim`/discard does, and that needs
  node-side (`sudo fstrim`) or a privileged pod (blocked by baseline PSA).
- Passwordless `sudo` for `lvs`/`vgs`/`lvextend` works on wyrm2 — node-side
  `lvextend` / `fstrim` is available if PVC recreation is undesirable.
