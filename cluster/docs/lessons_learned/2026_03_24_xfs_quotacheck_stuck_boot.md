# XFS Quotacheck Blocking Boot After Unclean Shutdown

**Date**: 2026-03-24
**Status**: Resolved (node recovered), prevention pending

## Summary

`talos-pve-cp-0` was stuck in `STAGE: Booting` for 29+ hours. The boot sequencer was
blocked at phase 5/9 (`mountEphemeralPartition`) because XFS quotacheck on the 300 GB
EPHEMERAL partition (`/dev/sda5`) never completed.

## Root Cause

1. **Trigger**: The VM was hard-stopped (`qm stop 10000`) on 2026-03-21, causing an
   unclean shutdown. Subsequent `qm stop`+`qm start` recovery attempts on Mar 21 and
   Mar 23 perpetuated the same issue.

2. **XFS quotacheck**: After unclean shutdown, XFS resets quota accounting flags during
   journal recovery. On the next mount, XFS detects the reset flags and runs a full
   quotacheck — walking every inode on the filesystem to rebuild quota data.

3. **Pathological duration**: The quotacheck on a 300 GB EPHEMERAL partition ran for
   29+ hours without completing. Kernel stack traces showed `xfs_qm_dqusage_adjust` →
   `xfs_iwalk_ag_recs` with slab allocation pressure (`___slab_alloc`). The 8 GB VM
   RAM combined with a large inode count (container images) likely caused the scan to
   thrash or deadlock.

4. **Cascade**: Without EPHEMERAL mounted, the Talos boot sequencer could not proceed.
   CRI never registered → Nebula/iscsid couldn't start → no etcd/kubelet/apiserver →
   node stuck NotReady.

## Key Symptoms

- Dashboard: `STAGE: Booting`, `READY: False`, all K8s components `n/a`
- `talosctl services`: no kubelet/etcd; `ext-nebula` and `ext-iscsid` waiting for
  `cri` to be registered
- Nebula IP (`10.42.0.10`) unreachable, VLAN IP (`10.2.1.1`) reachable
- dmesg: `XFS (sda5): Quotacheck needed: Please wait.` with no completion message

## Resolution

1. Wiped EPHEMERAL data via `talosctl reset --system-labels-to-wipe EPHEMERAL --reboot
--graceful=false`
2. GPT partition drop failed ("device or resource busy" — kernel still had sda5 open
   from stuck quotacheck), but the partition data was zeroed
3. Hard-reset VM via `qm reset 10000`
4. Node booted with fresh EPHEMERAL, reformatted it, and rejoined the cluster
5. etcd got a new member ID and replicated from the 2 healthy VPS peers

## Why `qm stop` is Dangerous

`qm stop` sends SIGKILL to the QEMU process — the guest has no opportunity to flush
filesystem caches or cleanly unmount. This is equivalent to pulling the power cable.

`qm shutdown` sends ACPI shutdown, giving the guest OS time to shut down gracefully.
Talos handles ACPI shutdown correctly.

## Prevention

1. **Never use `qm stop` for Talos VMs** — always use `qm shutdown` for graceful ACPI
   shutdown. If the guest doesn't respond to ACPI within a timeout, investigate why
   rather than force-killing.

2. **Investigate disabling XFS project quotas on EPHEMERAL** — Talos enables XFS quotas
   by default for container resource accounting. If the quotas aren't actively used for
   enforcement (only for monitoring), mounting with `noquota` would eliminate this class
   of failure entirely. Needs upstream Talos investigation.

3. **Consider more VM RAM** — the 8 GB allocation may be marginal for a control plane
   node running quotacheck on a large filesystem. The slab allocation pressure in the
   stack traces suggests memory was a bottleneck.

## Timeline

1. 2026-03-21 20:19 — First `qm stop` of VM 10000 (trigger unknown)
2. 2026-03-21 20:21 — Second `qm stop`+`qm start` (recovery attempt)
3. 2026-03-21 22:37 — Another `qm start`
4. 2026-03-23 17:02 — Manual `qm stop`+`qm start` recovery attempt from wyrm2
5. 2026-03-24 00:03 — Node boots, quotacheck starts, boot stuck at phase 5/9
6. 2026-03-25 05:51 — EPHEMERAL wiped via `talosctl reset`, VM hard-reset
7. 2026-03-25 05:53 — Node boots clean, rejoins cluster, becomes Ready
