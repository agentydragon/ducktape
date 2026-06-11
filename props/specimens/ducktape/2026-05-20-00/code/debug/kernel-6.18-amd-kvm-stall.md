# Kernel 6.18 `pv_native_safe_halt` Stall on AMD KVM

## Status: FIXED — `clearcpuid=510` on host eliminates all stalls

Linux kernel 6.18 has a bug in the KVM paravirtualized idle halt path
(`pv_native_safe_halt`) that causes periodic CPU stalls on AMD hosts. Reproduces on a
completely idle VM within 38 seconds of boot. Kernel 6.12 is unaffected on the same host.

**Host**: Proxmox 8 on AMD Ryzen 9 9950X3D (Zen 5), `cpu: host` passthrough.
**Workaround**: `echo 0 > /sys/module/kvm/parameters/halt_poll_ns` on the host.
**Upstream**: Red Hat Bugzilla #2448303 (NEW, unresolved). Fedora discussion:
<https://discussion.fedoraproject.org/t/kvm-guests-become-unstable-on-6-18-kernel/182870>

## Workaround: Disable KVM Halt Polling (2026-03-30)

Setting `halt_poll_ns=0` on the host **eliminates idle stalls** (idle VM stable for 2+
minutes). However, **stalls still occur under real workload** (etcd, kubelet, apiserver
booting). There are likely multiple bug paths in the 6.18 AMD KVM code.

**Idle test**: Talos v1.12.3 (kernel 6.18.8) VM with `halt_poll_ns=0` on atlas:
1m55s uptime, CPU 0.3%, zero stalls, clean boot. Same kernel with default
`halt_poll_ns=200000` stalls within 38 seconds.

**Real workload test**: Same VM with `halt_poll_ns=0`, configured to join the cluster
(etcd + kubelet + apiserver). Stalled at 4m1s — NMI "unknown reason 30 on CPU 3" in
a `seq_read_iter` / `ufs_read` path. CPU 98.3%. The halt polling workaround is
insufficient when the VM is under load.

**Conclusion**: `halt_poll_ns=0` fixes one stall path (`pv_native_safe_halt` idle loop)
but there's at least one more bug in kernel 6.18 on AMD KVM that triggers under load.
The workaround alone is not sufficient for production use.

**Root cause (partial)**: KVM halt polling on AMD Zen 5. When a vCPU executes HLT
(idle), the host KVM module "polls" briefly before halting the vCPU. The polling
implementation in kernel 6.18 has a bug on AMD that causes the vCPU to get stuck.

**Apply**:

```bash
# Immediate (non-persistent):
echo 0 > /sys/module/kvm/parameters/halt_poll_ns

# Persistent (survives reboot) — add to host kernel cmdline or modprobe.d:
echo "options kvm halt_poll_ns=0" > /etc/modprobe.d/kvm-halt-poll.conf
```

**Trade-off**: Disabling halt polling may slightly increase VM exit latency for short
idle periods (microseconds). In practice, the impact is negligible for server workloads.
The default 200μs polling window is an optimization, not a requirement.

## Guest-Side Root Cause: TSA Mitigation (kernel ~6.17+, AMD-only)

Source code analysis (2026-03-31) found the guest-side change that makes kernel 6.18
different from 6.12 on AMD KVM:

**TSA (Transient Scheduler Attack) mitigation**. All AMD Zen KVM guests get
`X86_BUG_TSA` forced on (for live migration safety). This enables `cpu_buf_idle_clear`,
which executes a **VERW instruction** (microcode buffer flush) before every `sti; hlt`
in the idle loop (`native_safe_halt()`).

```
v6.12 AMD guest idle:  sti; hlt                    (no VERW)
v6.18 AMD guest idle:  VERW [mem]; sti; hlt        (VERW before every HLT)
```

The VERW triggers a microcode-assisted CPU buffer flush. This is the **only behavioral
difference** in the idle path between 6.12 and 6.18 on AMD. On Intel, `mds_idle_clear`
was already enabled in 6.12 (for MDS/MMIO), so Intel guests already had VERW before
HLT — explaining why Intel KVM hosts are unaffected.

**Interaction with host's INTERCEPT_IDLE_HLT**: The VERW + HLT sequence may cause the
host's new `INTERCEPT_IDLE_HLT` code path to mishandle the exit. Or the VERW itself may
cause a VMEXIT that confuses the subsequent HLT intercept logic.

**Guest-side workaround**: `tsa=off` kernel parameter would disable the VERW. But Talos
uses SDBoot/UKI, so kernel params can't be overridden without a custom image.

## NMI "Unknown Reason 30" — Explained

The "unknown reason 30" NMIs are **harmless and expected** on KVM. KVM injects NMIs via
V_NMI_PENDING (or direct VMCB event injection), delivering interrupt vector 2 to the
guest. The guest reads port 0x61 to identify the NMI source, but:

- Port 0x61 is QEMU's PC speaker emulation — never sets NMI reason bits (6-7)
- "Reason 30" = `0x30` = PIT channel 2 output + refresh toggle (idle PIT state)
- With `nmi_watchdog=0` in the guest, no NMI_LOCAL handler claims the NMI
- KVM provides no paravirt mechanism to communicate NMI source to the guest

The NMI is legitimately injected by the hypervisor. The "unknown reason" message is
cosmetic, not indicative of a bug in the NMI path itself.

## Host-Side Factor: INTERCEPT_IDLE_HLT Missing from Fastpath

Source code analysis found a **confirmed code asymmetry**: `SVM_EXIT_IDLE_HLT` is NOT
in the `svm_exit_handlers_fastpath` switch — only `SVM_EXIT_HLT` is:

```c
// svm_exit_handlers_fastpath (svm.c ~4240):
case SVM_EXIT_HLT:              // ← handled in fastpath
    return handle_fastpath_hlt(vcpu);
// SVM_EXIT_IDLE_HLT (0xa6) — NOT present, falls to EXIT_FASTPATH_NONE
```

On Zen 5 with `INTERCEPT_IDLE_HLT`, every HLT exit goes through the **slow path**
(full `vcpu_run` loop + `svm_invoke_exit_handler`). With `INTERCEPT_HLT`, the fastpath
can do `EXIT_FASTPATH_REENTER_GUEST` without the full exit. This is likely an oversight
in the Idle HLT Intercept patches.

### Mechanical explanation of the stall

Two factors combine:

1. **Guest VERW before HLT** (TSA mitigation): The `VERW` microcode buffer flush before
   `sti; hlt` may cause a VMEXIT itself or change timing such that `V_INTR`/`V_NMI`
   arrive between VERW and HLT. The hardware's `INTERCEPT_IDLE_HLT` may incorrectly
   evaluate pending state during this window (Zen 5 microcode race).

2. **Missing fastpath**: Every `SVM_EXIT_IDLE_HLT` takes the slow path, amplifying
   any timing issue. Under load, the slow path interacts with scheduler preemption
   and halt polling differently than the fastpath.

### What each workaround fixes

| Workaround        | Idle stalls    | Load stalls | Mechanism                                 |
| ----------------- | -------------- | ----------- | ----------------------------------------- |
| `halt_poll_ns=0`  | Yes            | No          | Skips polling loop (stall was in polling) |
| `clearcpuid=510`  | Yes (expected) | Maybe       | Forces INTERCEPT_HLT + fastpath           |
| `tsa=off` (guest) | Yes (expected) | Expected    | Removes VERW before HLT                   |
| Host kernel 6.8   | Yes (expected) | Expected    | No INTERCEPT_IDLE_HLT at all              |

### Confidence assessment

- **High**: `clearcpuid=510` will fix idle stalls (avoids INTERCEPT_IDLE_HLT)
- **Medium**: `clearcpuid=510` will fix load stalls (fastpath asymmetry is real, but
  load stalls could also involve VERW independent of halt intercept type)
- **Fallback**: host kernel 6.8 eliminates all 6.15+ kvm_amd changes (already installed)

## Host-Side Factor: AMD Idle HLT Intercept (kernel 6.15)

LKML search found a **new AMD KVM feature** merged in kernel 6.15 that changes how
KVM handles guest HLT instructions on AMD:

- **Patch**: <https://lore.kernel.org/kvm/20241022054810.23369-1-manali.shukla@amd.com/T/>
- **Author**: Manali Shukla (AMD)
- **Reviewer**: Sean Christopherson (KVM maintainer) — flagged nested support as
  "99% certain wrong", deferred to later
- **Phoronix coverage**: <https://www.phoronix.com/news/Linux-6.15-KVM>

The feature conditionally intercepts HLT based on pending `V_INTR` / `V_NMI` instead
of always intercepting. If there's a race where pending events are missed, the vCPU
sleeps when it shouldn't — causing exactly the stalls we observe in `pv_native_safe_halt`.

This landed in 6.15, between our known-good 6.12 and known-bad 6.18. The
`halt_poll_ns=0` workaround likely works because it changes the host-side behavior
around the HLT exit, sidestepping the buggy conditional intercept path.

**Next steps to confirm**: Test kernel 6.14 vs 6.15 guests (6.14 should be fine, 6.15
should stall). File on `kvm@vger.kernel.org` with bisect data + CC Manali Shukla and
Sean Christopherson.

## Host vs Guest Interaction (2026-03-31)

**Host kernel**: 6.17.13-1-pve (Proxmox). Has the Idle HLT Intercept feature (merged
6.15) in its kvm_amd module.

**Key observation**: v1.11.6 guest (kernel 6.12) runs clean on this host — zero NMIs,
zero stalls after 5+ minutes in maintenance mode. Same host, same kvm_amd module,
same `halt_poll_ns=0`. v1.12.3 guest (kernel 6.18) stalls within minutes.

**Conclusion**: The bug is an **interaction** between the guest kernel 6.18 and the
host's kvm_amd, not purely a host-side or guest-side bug. The guest kernel 6.18 does
something differently (new paravirt halt mechanism, different HLT usage pattern, or
different interrupt handling) that triggers the buggy host-side code path. Kernel 6.12
guests avoid the buggy path.

**kvm_amd parameters investigated**: No `idle_hlt_intercept` parameter exists. `vnmi=Y`
(read-only, can't test without module reload). `npt=Y` (read-only). Only
`dump_invalid_vmcb` is writable at runtime. Testing `vnmi=N` or `npt=N` requires
stopping all VMs and reloading kvm_amd — disruptive since wyrm2 runs on the same host.

**Load stall under `halt_poll_ns=0`**: VM 10000 (v1.12.3) stalled at 2m3s with a TLB
flush stack trace (`flush_tlb_mm_range` → `do_wp_page` → `__handle_mm_fault`). NMI on
CPU 3. This is yet another arbitrary kernel path, confirming the stall mechanism is
CPU-level, not subsystem-specific.

## Host Kernel Data (2026-03-31)

**Current host kernel**: `6.17.13-1-pve` (Proxmox)
**Available boot entries**: `6.17.13-1-pve`, `6.17.9-1-pve`, `6.8.12-18-pve`
**Upgradable**: `proxmox-kernel-6.17` → `6.17.13-2`, `proxmox-kernel-6.8` → `6.8.12-20`

**Key code path** (from svm.c source analysis):

```c
if (!kvm_hlt_in_guest(vcpu->kvm)) {
    if (cpu_feature_enabled(X86_FEATURE_IDLE_HLT))
        svm_set_intercept(svm, INTERCEPT_IDLE_HLT);   // ← NEW path on Zen 5
    else
        svm_set_intercept(svm, INTERCEPT_HLT);         // ← old path
}
```

AMD Zen 5 (9950X3D) advertises `X86_FEATURE_IDLE_HLT`, so the host's kvm_amd uses
`INTERCEPT_IDLE_HLT` instead of the traditional `INTERCEPT_HLT`. No module parameter
exists to disable this — it's keyed off CPU feature detection.

**Critical test available**: Reboot atlas into kernel **6.8.12-18-pve** (pre-6.15, before
Idle HLT Intercept was merged). If guest stalls disappear on host kernel 6.8, it confirms
the bug is in the **host's** kvm_amd, not the guest kernel. The guest kernel 6.18 merely
triggers the host-side bug; an older host avoids it entirely.

This would also confirm the `X86_FEATURE_IDLE_HLT` / `INTERCEPT_IDLE_HLT` code path as
the root cause, since it doesn't exist in kernel 6.8.

## Targeted Fix: `clearcpuid=510` (2026-03-31)

LKML/source research found: `X86_FEATURE_IDLE_HLT` = bit 510 (word 15, bit 30). The
feature is auto-enabled by CPU feature detection (`cpu_feature_enabled()`), with **no
module parameter** to disable it. But the kernel supports `clearcpuid=N` boot parameter
to mask individual CPUID bits.

**`clearcpuid=510`** on the **host** kernel cmdline forces the old `INTERCEPT_HLT` path
instead of `INTERCEPT_IDLE_HLT`. This is the most targeted possible fix — disables only
the Idle HLT Intercept while keeping everything else on kernel 6.17.

The two bugs may be coupled via the V_NMI interaction:

- Idle HLT Intercept suppresses VMEXITs when `V_NMI_PENDING` is set
- If V_NMI pending state is incorrect, spurious NMIs are delivered to the guest
- This explains both: idle stalls (HLT exit suppressed) and load NMIs (spurious NMI
  injection from incorrectly pending V_NMI)

**Alternative**: `kvm_amd.vnmi=0` would disable vNMI entirely (requires module reload).
Could fix the "unknown reason 30" NMIs independently. But `clearcpuid=510` is cleaner
since it targets the root feature.

**Test**: Add `clearcpuid=510` to atlas kernel cmdline, reboot, verify both idle and
load stalls are gone.

## Bisect (2026-03-30)

Two throwaway VMs on atlas, identical config (4 cores, 4 GiB, `cpu: host`, virtio-gpu,
`balloon: 0`, no cluster, no workload):

- **Talos v1.11.6 (kernel 6.12.62)**: Clean boot, stable, no issues.
- **Talos v1.12.3 (kernel 6.18.8)**: **RCU stall + NMI within 38 seconds of boot**
  while idle. CPU stuck in `pv_native_safe_halt` (idle loop). NMI sent from CPU 3 to
  CPU 2. No workload running.

The stall is in `pv_native_safe_halt` — the KVM paravirt halt path. All earlier stalls
observed in production (page allocator, XFS, slab) were just whatever code happened to
be running when the CPU came out of the broken halt.

## Symptoms

- Periodic CPU stalls every ~38s to ~5 min (depends on load)
- RCU stalls: `rcu_sched detected stalls on CPUs/tasks`
- NMIs: `Uhhuh. NMI received for unknown reason N on CPU M`
- Health check `DeadlineExceeded` across all services
- Stack traces in arbitrary kernel paths (page allocator, XFS, slab, idle)
- CPU usage spikes to 90%+
- Node recovers after each stall but etcd/kubelet health checks fail during it

## What was ruled out

| Hypothesis              | Test                                  | Result        |
| ----------------------- | ------------------------------------- | ------------- |
| QXL VGA driver          | Switched to virtio-gpu                | Still stalls  |
| Memory balloon          | Disabled balloon                      | Still stalls  |
| Host NMI watchdog       | `nmi_watchdog=0` on host              | Still stalls  |
| CPU passthrough         | Changed to `cpu: x86-64-v3`           | Still stalls  |
| `init_on_alloc=1`       | Can't disable (baked into Talos UKI)  | N/A           |
| VM instance state       | Fresh VM, fresh disk                  | Still stalls  |
| Resource pressure       | 43% RAM, <1% steal, 20% CPU           | Not the cause |
| Fedora `migratable=off` | `cpu: x86-64-v3` doesn't pass through | Still stalls  |

## CRITICAL UPDATE (2026-03-31): Kernel 6.12 ALSO Stalls Under Load

NixOS kernel 6.12.78 guest on same host (atlas, kvm_amd 6.17, `halt_poll_ns=0`, no
`clearcpuid=510`) shows **identical stall pattern** during boot: RCU preempt stall,
NMIs sent to debug stalled CPUs, page fault in `folio_alloc_mpol_noprof` →
`do_anonymous_page`. Stall at ~711s into boot.

This means:

- **The bug is NOT kernel 6.18 guest-specific** — 6.12 guests stall too under load
- **TSA VERW hypothesis was wrong** (6.12 has no TSA mitigation)
- The bug is purely **host-side** (`INTERCEPT_IDLE_HLT` in kvm_amd 6.17)
- wyrm2 (also 6.12, same host) is stable — difference is 32 cores / 96GB vs 4 cores / 2GB
- **`clearcpuid=510`** on the host remains the correct fix (disables `INTERCEPT_IDLE_HLT`)
- The earlier bisect showing "6.12 clean, 6.18 stalls" was misleading — idle VMs
  don't trigger the bug, only VMs under load

## Test Results

| Guest                   | Kernel | Host kernel | halt_poll_ns | Workload   | Stalls?        | Verified?                         |
| ----------------------- | ------ | ----------- | ------------ | ---------- | -------------- | --------------------------------- |
| Talos v1.12.3           | 6.18.8 | 6.17.13-pve | 200000       | idle       | **YES (38s)**  | Talos console (stack traces)      |
| Talos v1.12.3           | 6.18.8 | 6.17.13-pve | 0            | idle       | No (2min)      | Talos console (clean)             |
| Talos v1.12.3           | 6.18.8 | 6.17.13-pve | 0            | etcd+k8s   | **YES (4min)** | Talos console (NMI + stack trace) |
| Talos v1.11.6           | 6.12   | 6.17.13-pve | 200000       | idle       | No (2min)      | Talos console (clean)             |
| wyrm2 NixOS             | 6.12   | 6.17.13-pve | 200000       | k8s worker | No (7 days)    | **dmesg verified** (zero NMIs)    |
| Talos v1.12.3 (Hetzner) | 6.18.8 | Intel       | default      | etcd+k8s   | No (weeks)     | Production (healthy)              |
| Fedora 42               | 6.14   | 6.17.13-pve | 0            | idle       | No (60s)       | Console only (**NOT dmesg**)      |
| Arch Linux              | 6.19.8 | 6.17.13-pve | 0            | idle       | No (5min)      | Console only (**NOT dmesg**)      |

**Key gap**: Non-Talos VMs (Fedora, Arch) couldn't be verified via dmesg — no login
access configured. Need cloud-init with SSH keys for proper verification.

### Planned test matrix

Need a proper scaffold: cloud-init with SSH keys, `stress-ng` for load, automated
dmesg/NMI collection after N minutes. Variables to test:

| Variable                  | Values to test                                       |
| ------------------------- | ---------------------------------------------------- |
| Guest kernel              | 6.12, 6.14, 6.15, 6.17, 6.18, 6.19                   |
| `halt_poll_ns` (host)     | 0, 200000                                            |
| `clearcpuid=510` (host)   | yes, no                                              |
| `tsa=off` (guest)         | yes, no (only testable on non-Talos guests)          |
| `mitigations=off` (guest) | yes, no                                              |
| Workload                  | idle, `stress-ng --cpu 4`                            |
| Distro                    | Talos, Fedora, Arch (to isolate Talos kernel config) |

**Data to collect per test**: `cat /proc/interrupts | grep NMI`, `dmesg | grep -c rcu`,
`dmesg | grep -c nmi`, uptime, CPU usage. Automated via SSH after 5-minute soak.

## Impact on cluster

The Proxmox control plane node (`talos-pve-cp-0`) runs Talos v1.12.3. The stalls cause:

1. etcd health check failures → intermittent quorum issues
2. Operator leader election losses → restart cascades → pod churn on wyrm2
3. Pod churn → Chrome `ERR_NETWORK_CHANGED` on wyrm2 (see below)

Cascading failure on 2026-03-30: removing pve-cp-0 during debugging left 2-member etcd.
VPS nodes (no `NoSchedule` taint) absorbed workload pods → OOM → nebula tunnel broke →
etcd no leader → full cluster outage. See <debug/wyrm2-chrome-network-changed.md>.

## Fix Applied: `clearcpuid=510` on host (2026-03-31)

Applied in `ansible/atlas.yaml` kernel cmdline + `halt_poll_ns=0` in modprobe.d.
Atlas rebooted 2026-03-31. **Confirmed working**: NixOS 6.12 test VM boots in <90s
with 0 NMIs (previously stalled for 12+ min). talos-pve-cp-0 reached `Ready` for
the first time in days. Cluster fully recovered with 3 etcd members.

## Investigation timeline

- **2026-03-23**: First NMI incident on talos-pve-cp-0 (incident 1)
- **2026-03-25**: Second NMI (incident 2), third incident as RCU stall (no NMI)
- **2026-03-25**: Ruled out QXL, balloon, NMI watchdog, resource pressure, `ostype`
- **2026-03-26**: Ruled out instance-specific state (fresh VM reproduces)
- **2026-03-26**: Found Fedora report (Bugzilla #2448303), `cpu: x86-64-v3` didn't help
- **2026-03-26**: Confirmed `init_on_alloc=0` can't be set (Talos UKI/SDBoot)
- **2026-03-26**: Confirmed v1.12.6 (kernel 6.18.18) has no KVM fixes
- **2026-03-30**: Cluster outage from VPS OOM during debugging
- **2026-03-30**: v1.11.6 downgrade blocked (K8s 1.35.1 incompatible)
- **2026-03-30**: **Bisect confirmed**: kernel 6.18 stalls in `pv_native_safe_halt`
  within 38s on idle VM. Kernel 6.12 fine.

## After Reboot Checklist — COMPLETED 2026-03-31

All items verified:

1. ✅ `clearcpuid=510` in `/proc/cmdline`
2. ✅ `halt_poll_ns=0` persisted from modprobe.d
3. ✅ NixOS 6.12 test VM: boots in <90s, 0 NMIs, SSH works
4. ✅ talos-pve-cp-0: `Ready`, etcd healthy
5. ✅ VPS nodes uncordoned
6. N/A — `clearcpuid=510` fixed everything, no fallback needed

## TODOs

- [ ] Add `NoSchedule` taints to VPS control plane nodes (prevent OOM cascade)
- [ ] File upstream bug on `kvm@vger.kernel.org` with bisect data + `clearcpuid=510`
      finding, CC Manali Shukla and Sean Christopherson
- [ ] File Talos issue linking to the upstream bug
- [ ] Monitor Red Hat Bugzilla #2448303 for upstream fix
- [ ] Remove `clearcpuid=510` and `halt_poll_ns=0` workarounds once fix lands

## Related

- <debug/pve-cp0-notready-2026-03-23/README.md> — original NMI incident investigation
- <debug/wyrm2-chrome-network-changed.md> — Chrome ERR_NETWORK_CHANGED (downstream effect)
- <debug/atlas/wyrm2-freezes.md> — wyrm2 QXL TTM bug (different issue, resolved)
