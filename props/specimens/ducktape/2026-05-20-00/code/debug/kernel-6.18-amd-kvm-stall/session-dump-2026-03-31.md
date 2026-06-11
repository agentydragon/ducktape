# Session Dump: Kernel 6.18 AMD KVM Stall Investigation

**Date**: 2026-03-25 through 2026-03-31
**Branch**: `devel`
**Working directory**: `/home/agentydragon/code/ducktape`

## What Was Done

### Root Cause Investigation

Started with Chrome `ERR_NETWORK_CHANGED` on wyrm2, traced through:

1. Pod churn from stale TF lock leases (fixed by deleting 7 `lock-tfstate-default-*` Leases)
2. Operator restart cascades from etcd instability
3. `talos-pve-cp-0` periodic CPU stalls → etcd health failures
4. Bisected: kernel 6.18 guest stalls on AMD KVM host, kernel 6.12 fine
5. `halt_poll_ns=0` fixes idle stalls but NOT load stalls
6. Source code analysis: guest TSA VERW mitigation + host INTERCEPT_IDLE_HLT missing fastpath
7. `clearcpuid=510` prepared but NOT YET TESTED (requires atlas reboot)

### Key Findings

- **Guest side**: Kernel 6.18 adds TSA (Transient Scheduler Attack) mitigation for AMD Zen KVM guests, forcing VERW instruction before every `sti; hlt`. Not present in 6.12.
- **Host side**: `INTERCEPT_IDLE_HLT` (new in 6.15) missing from `svm_exit_handlers_fastpath` — always takes slow path. Confirmed code asymmetry.
- **NMI "unknown reason 30"**: Harmless — KVM injects NMIs via V_NMI but QEMU port 0x61 never indicates source.
- Arch Linux kernel 6.19.8 guest appeared stable but **NOT verified via dmesg** (couldn't login).

### Cluster Outage (2026-03-30)

Removing pve-cp-0 during debugging → 2-member etcd → VPS OOM (no NoSchedule taint) → nebula tunnel broke → full cluster outage. Recovered by rebooting + cordoning VPS nodes.

### Files Created/Modified

- `debug/kernel-6.18-amd-kvm-stall.md` — main investigation doc (bisect, code analysis, workaround table)
- `debug/wyrm2-chrome-network-changed.md` — Chrome ERR_NETWORK_CHANGED (trimmed, references kernel bug doc)
- `debug/pve-cp0-notready-2026-03-23/README.md` — updated status to reference main doc
- `debug/nebula-stale-tunnel-after-lighthouse-reboot.md` — nebula tunnel recovery failure
- `debug/kernel-6.18-amd-kvm-stall/run_test_matrix.py` — test matrix scaffold (partially working)
- `debug/kernel-6.18-amd-kvm-stall/collect_guest_data.sh` — guest data collection script
- `ansible/atlas.yaml` — added `clearcpuid=510` to kernel cmdline + `halt_poll_ns=0` modprobe.d
- `cluster/terraform/main/variables.tf` — added `proxmox_talos_version` variable
- `cluster/terraform/main/proxmox-nodes.tf` — wired to use `proxmox_talos_version`

### Git Commits (on devel, pushed)

Key commits (latest first):

- `d98e09451` — fix test matrix (static IPs, cloud-init IDE drive, vmbr0)
- `5c9384500` → `595b1a976` — TSA mitigation + fastpath analysis + NMI explanation
- `4ed2cd95a` → `7d9f9d909` — clearcpuid=510 in ansible + investigation consolidation
- `57860abb4` — original squashed investigation commit
- Multiple intermediate commits for halt_poll_ns, bisect results, etc.

## Incomplete Work (CRITICAL)

### 1. Atlas reboot with `clearcpuid=510` — NOT DONE

`ansible/atlas.yaml` has `clearcpuid=510` in kernel cmdline and `halt_poll_ns=0` in modprobe.d.
Need to:

```bash
cd ansible && ansible-playbook atlas.yaml --tags gpu_passthrough,iommu,kvm -l atlas
# Then reboot atlas (all VMs restart)
# After reboot: follow checklist in debug/kernel-6.18-amd-kvm-stall.md
```

### 2. Test matrix not producing full results

- Talos 6.12: **works** (0 NMIs confirmed via talosctl)
- Talos 6.18: unreachable (stalls — expected, but no dmesg captured)
- Fedora 42: unreachable in batch mode despite working in manual single-VM test
- Arch: doesn't boot with OVMF

**Needs**: debug Fedora cloud-init in batch, or build custom NixOS test images with
controllable kernel versions (user suggestion).

### 3. pve-cp-0 stalling on current host

VM 10000 exists and is running Talos v1.12.3 but stalling (halt_poll_ns=0 applied but
clearcpuid=510 not yet — requires host reboot). Currently a stally 3rd etcd member.

### 4. VPS nodes still cordoned

```bash
kubectl uncordon talos-vps-cp-0
kubectl uncordon talos-vps-cp-1
```

Do this after pve-cp-0 is stable (post-reboot).

### 5. NoSchedule taints for VPS nodes — NOT DONE

VPS CPs lack `NoSchedule` taints. During the outage, workload pods OOMed them.
Need to add taints in TF config patches for VPS nodes.

### 6. Upstream bug reports — NOT FILED

- `kvm@vger.kernel.org` — bisect data + clearcpuid=510 finding, CC Manali Shukla + Sean Christopherson
- Talos issue linking to the upstream bug
- Red Hat Bugzilla #2448303 — comment with our findings

### 7. Nebula stale tunnel — NOT FILED

After lighthouse reboot, non-lighthouse peers don't re-handshake.
See `debug/nebula-stale-tunnel-after-lighthouse-reboot.md`.
File at https://github.com/slackhq/nebula/issues

## Context for Successor

- Main investigation: `debug/kernel-6.18-amd-kvm-stall.md`
- After-reboot checklist: search for "After Reboot Checklist" in that file
- Cluster state: 2 VPS CPs (cordoned), wyrm2 (worker), pve-cp-0 (stalling), rugged (NotReady)
- Test matrix: `debug/kernel-6.18-amd-kvm-stall/run_test_matrix.py`
- Build/verify: `pre-commit run --all-files` (shfmt may fail — nix env issue)
- Key upstream refs:
  - Red Hat Bugzilla #2448303
  - Fedora discussion: https://discussion.fedoraproject.org/t/kvm-guests-become-unstable-on-6-18-kernel/182870
  - AMD Idle HLT Intercept patch: https://lore.kernel.org/kvm/20241022054810.23369-1-manali.shukla@amd.com/T/
