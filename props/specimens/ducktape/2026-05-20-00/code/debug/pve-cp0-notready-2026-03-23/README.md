# talos-pve-cp-0 NMI incidents

## Status: Root cause identified — AMD Idle HLT Intercept bug in host kvm_amd

**Full investigation**: <debug/kernel-6.18-amd-kvm-stall.md>

**Root cause**: AMD Idle HLT Intercept feature (merged kernel 6.15, `X86_FEATURE_IDLE_HLT`
= CPUID bit 510). The host's kvm_amd on Zen 5 uses `INTERCEPT_IDLE_HLT` instead of
`INTERCEPT_HLT`, causing guest vCPU stalls and spurious NMI injection. Interaction between
host kvm_amd (6.17) and guest kernel 6.18.

**Fix**: `clearcpuid=510` on host kernel cmdline (disables the feature). Applied in
`ansible/atlas.yaml`. Applied and rebooted 2026-03-31. **Fix confirmed working.**

## Incident 1 — 2026-03-23

### Timeline

- **2026-03-23 ~05:57 UTC**: Kernel NMI on CPU 1 + `asm_exc_page_fault` (visible in
  console screenshot). Kernel prints "Dazed and confused, but trying to continue."
- **2026-03-23 05:58 UTC**: Last `kubeaccess.EndpointController` activity in console logs.
- **2026-03-23 06:27 UTC**: Last kubelet heartbeat.
- **2026-03-23 06:31 UTC**: Last kubelet lease renewal (`2026-03-23T06:31:30Z`).
- **2026-03-23 06:32 UTC**: Node transitions to `NotReady`/`Unknown`.
- **2026-03-23 ~16:30 UTC**: Discovered during TF consolidation work.
- **2026-03-24 ~00:03 UTC**: VM rebooted (`qm stop` + `qm start`). VLAN IP recovered.
  Nebula + kubelet stuck — boot never completes (see "Post-reboot boot hang" below).
- **2026-03-24 ~00:07 UTC**: Machine config reapplied via `tofu apply -target=talos_machine_configuration_apply.proxmox`.
  No effect — still booting.

The NMI at 05:57 preceded network death by ~30 minutes. Kubelet continued heartbeating
until 06:31, then stopped. This suggests progressive degradation, not instant failure.

NMI reason code: `unknown reason 10`.

## Incident 2 — 2026-03-25

### Timeline

- **2026-03-25 ~16:37 UTC**: Kernel NMI on CPU 1 + `asm_exc_page_fault` (same pattern).
  "NMI received for unknown reason 30 on CPU 1." Stack trace identical to incident 1.
- Health checks briefly failed (`context deadline exceeded`), then recovered.
- Node stayed up — `READY: False` but services (kubelet, apiserver, controller-manager,
  scheduler) all show healthy. Less severe than incident 1 (no TX path wedge this time).
- VM uptime at time of screenshot: 6h3m20s.

NMI reason code: `unknown reason 30` (different from incident 1's `10`).

### Key differences from incident 1

- Node did **not** lose network — recovered on its own after brief health check failures.
- Different NMI "reason" code (30 vs 10) — these are the ISR status port values, both
  meaning "unknown source". The different values may indicate different perf counter
  overflow conditions.

## NMI investigation

### What we know

**Guest kernel log:**

```
kern: warning: asm_exc_page_fault+0x26/0x30
kern: warning: RIP: 0033:0x4864eb
kern: warning: Code: ... <f3> 44 0f 7f 3f ...   (vmovdqu ymm — AVX memory store)
kern: warning: RSP: 002b:000000c0780c68a8 EFLAGS: 00010246
kern:  emerg: Uhhuh. NMI received for unknown reason 10 on CPU 1.
kern:  emerg: Dazed and confused, but trying to continue
kern: warning: clocksource: Long readout interval, skipping watchdog check
```

- `RIP: 0033:0x4864eb` — userspace (ring 3, segment 0x33). A userspace process page-faulted.
- The code bytes at the fault contain `0x44 0x0f 0x7f` = `vmovdqu` (AVX memory stores).
  This is a Go runtime memcpy/memmove using AVX instructions.
- "Unknown reason 10" — NMI source unidentified by the guest kernel (not watchdog, not
  I/O check, not PCI SERR).
- Clocksource warning indicates timing instability concurrent with the NMI.

**Host-side (atlas):**

- **Zero host-side evidence**: `journalctl -k` for the 05:50-06:10 window shows only
  apparmor/cupsd noise. No MCE, no NMI, no KVM errors. The NMI was entirely guest-internal.
- No MCE log (`mcelog` not available).
- Host CPU: **AMD Ryzen 9 9950X3D** (Zen 5, 3D V-Cache).
- Host NMI counts high across all cores (checked 2026-03-25, uptime 3d14h):
  6K-20K per core, cores 24-25 highest at ~20K. Source: `nmi_watchdog=1` (Linux default).
  The host NMI watchdog programs a perf counter on every core for soft lockup detection.

**KVM configuration (host):**

| Parameter         | Value | Significance                                   |
| ----------------- | ----- | ---------------------------------------------- |
| `vnmi`            | `Y`   | Virtual NMI enabled (AMD hardware feature)     |
| `avic`            | `N`   | Advanced Virtual Interrupt Controller disabled |
| `intercept_smi`   | `Y`   | SMIs intercepted                               |
| `kvm.ignore_msrs` | `N`   | Guest MSR access not silently ignored          |
| `npt`             | `Y`   | Nested Page Tables enabled                     |

**QEMU config:**

- `cpu: host,+kvm_pv_eoi,+kvm_pv_unhalt` — full CPU passthrough, guest sees all
  Zen 5 performance counters and MSRs.
- No explicit PMU disable (`-cpu host,pmu=off` not set).
- No QEMU watchdog configured.

### NMI source hypothesis

**Revised 2026-03-25**: The most likely source is the **host kernel NMI watchdog**
leaking PMIs into the guest via KVM vNMI.

**Key finding**: `nmi_watchdog=1` on atlas (Linux default, not Proxmox-specific). The
host kernel's NMI watchdog programs a perf hardware counter on every CPU that generates
periodic PMIs to detect soft lockups. Host `/proc/interrupts` shows 6K-20K NMIs across
all 32 cores — consistent with continuous watchdog ticking, not a single hardware event.

With `vnmi=Y` (AMD hardware vNMI), KVM can inject these host-originated PMIs as virtual
NMIs into guest vCPUs. The guest kernel doesn't recognize them because it didn't program
the counter ("unknown reason").

Evidence:

1. No host-side NMI/MCE in journald — rules out hardware platform NMI.
2. Host `nmi_watchdog=1` — generates continuous PMIs on all cores via perf counters.
3. Host NMI counts: 6K-20K per core (cores 24-25 highest at ~20K), distributed across
   all CPUs. This is the watchdog, not a one-off event.
4. KVM `vnmi=Y` — AMD hardware feature that injects NMIs into guest vCPUs.
5. `cpu: host` with no `pmu=off` — guest sees host perf counters.
6. Guest kernel can't identify the NMI source because it didn't program the counter.
7. `perf_event_paranoid=4` on host — but the watchdog runs in kernel space, bypassing this.

**Previous hypothesis** (guest-initiated PMI from containerd/kubelet profiling) is less
likely given that the host watchdog is the obvious continuous source of PMIs.

**Consequence** (incident 1): The NMI likely interrupted a critical section in the
virtio-net driver or network softirq, leaving a spinlock held or a TX queue stuck.
Userspace and the kernel scheduler continued, but no frames were ever transmitted again.
Incident 2 was less severe — brief health check failures, then recovery.

### Alternative hypotheses

- **Guest-initiated PMI**: Guest perf subsystem (or Go runtime profiling) programmed a
  counter that overflowed. Less likely given the host watchdog is already generating PMIs.
- **Spurious vNMI from KVM AMD**: The `vnmi` implementation on Zen 5 may have a bug
  that generates spurious NMIs under certain conditions (e.g., vCPU migration between
  physical cores during V-Cache scheduling).
- **3D V-Cache topology interaction**: The 9950X3D has asymmetric CCDs (one with V-Cache,
  one without). vCPU scheduling across CCDs during cache-heavy workloads could trigger
  timing anomalies that manifest as unexpected NMIs.

### Host logging situation

- **journald**: Persists to disk, 1.2GB, oldest entry 2026-02-03. The 05:50-06:10 UTC
  window is present — but contains only cupsd apparmor spam. No kernel NMI/MCE entries
  because the NMI was guest-internal (KVM vNMI injection, no host kernel involvement).
- **dmesg ring buffer**: Volatile (RAM-only), default ~1MB. Rotated out by cupsd apparmor
  spam within hours. Even if the host kernel had logged something, it would be gone.
- **mcelog**: Not installed on Proxmox.

The fundamental limitation: KVM vNMI injects NMIs directly into the guest vCPU without
host kernel involvement. The host has no record of guest-internal NMIs. Host-side logging
improvements won't help for this class of issue — we'd need KVM tracepoints
(`/sys/kernel/debug/kvm/`) or `perf kvm` recording.

### What we can't determine (evidence lost)

- Exact process that page-faulted (RIP 0x4864eb — Go binary, but which one?)
- Whether a perf counter was actually programmed (perf state lost on reboot)
- Whether virtio-net TX queue state was corrupted (lost on reboot)
- Host NMI/MCE state at incident time (dmesg ring buffer rotated by cupsd spam)

## Network diagnostics (pre-reboot)

- `ping 10.2.1.1` from atlas: 100% loss, ARP: `FAILED`
- `ping 10.42.0.10` from all Nebula nodes: 100% loss
- `tcpdump -i tap10000i0`: ARP requests arrive at tap, **zero frames transmitted by VM**
- Bridge FDB: MAC `9a:5a:81:83:df:a7` (stale, not the VM's `bc:24:11:0d:5e:91`)
- IPv6 NDP: VM's MAC seen as `STALE` — IPv6 worked at some point before failure
- Talos console: all services "healthy", "Connectivity: OK" — userspace unaware of TX death

## Post-reboot boot hang

After `qm stop` + `qm start`:

- VLAN IP `10.2.1.1` recovered immediately (responds to ping from atlas)
- Talos boots to "Booting" stage but never reaches "Running"
- Containerd starts but CRI never registers → kubelet/etcd never start → Nebula extension waits
- Console shows XFS stack traces (`xfs_qm_dqusage_adjust`, `xfs_iwalk`) and
  containerd health check timeouts
- Disk volumes all show `ready` in `talosctl get volumestatus`
- Machine config is present and correct (etcd CA, cluster config, etc.)
- 25+ minutes and still booting — no new log activity, CPU 0.3%
- Machine config reapply via Talos API had no effect

This may be a separate issue (XFS quota init on a large/dirty EPHEMERAL partition)
or the post-crash filesystem state is blocking boot.

## Talos capabilities for future diagnostics

Checked Talos v1.12 docs for relevant features:

- **Kernel crash dumps (kdump)**: Not supported. No pstore/ramoops either.
- **`talosctl dmesg` persistence**: RAM-only ring buffer, does NOT survive reboots.
- **Remote log forwarding**: Supported via machine config — can forward kernel logs to
  a remote syslog/Loki in real-time. This is the main way to preserve pre-crash logs.
- **Hardware watchdog**: Supported. Talos can configure `/sys/class/watchdog/watchdog0`
  via `WatchdogTimerConfig` resource. Talos pets the timer; if the system freezes, the
  hardware resets it. Proxmox/QEMU supports `i6300esb` watchdog.
- **Kernel args**: Customizable via `.machine.install.extraKernelArgs` (GRUB) or
  machine config patches. Can set `nmi_watchdog=0`, `unknown_nmi_panic=0`, etc.
- **perf/PMU tools**: Not available in Talos (no shell, minimal userspace).

**Actionable for this class of issue:**

1. Configure remote kernel log forwarding to Loki — captures NMI messages before reboot.
2. Enable hardware watchdog — auto-resets the VM if the kernel hangs after NMI.
3. Neither helps with post-mortem (no kdump), but at least we get the logs and auto-recovery.

## Prevention recommendations

### Immediate

1. **Disable guest PMU**: Change VM CPU config to `cpu: host,pmu=off`. This prevents
   the guest from seeing/programming performance counters. However, if the NMI source
   is the host watchdog (see revised hypothesis), this alone may not help — the host
   PMIs may still be injected via vNMI regardless of guest PMU state.
   Trade-off: no `perf` profiling inside the VM.

   ```bash
   # In proxmox-nodes.tf or via qm:
   cpu { type = "host"; flags = ["-pmu"] }
   # Or: ssh root@atlas "qm set 10000 -cpu 'host,pmu=off'"
   ```

1b. **Disable host NMI watchdog**: Set `nmi_watchdog=0` on atlas. This stops the host
kernel from programming perf counters for soft lockup detection, eliminating the
continuous PMI source. Proxmox docs recommend this for VM performance.
Trade-off: lose host soft-lockup detection (can still detect via other means).
**Status**: Not yet applied. Need to understand whether this actually prevents
vNMI injection into guests, or whether the vNMI path is independent.

```bash
# Immediate (non-persistent):
ssh root@atlas "echo 0 > /proc/sys/kernel/nmi_watchdog"
# Persistent: add nmi_watchdog=0 to kernel cmdline
```

2. **Remove cupsd from atlas**: cupsd apparmor spam fills the host dmesg ring buffer
   within hours, hiding real hardware events. cupsd has no purpose on a headless Proxmox
   server.

   ```bash
   ssh root@atlas "apt purge cups cups-daemon"
   ```

3. **Alert on node NotReady**: Prometheus alerting rule for
   `kube_node_status_condition{condition="Ready",status="true"} == 0 for 5m`.

### Medium-term

4. **Enable Talos hardware watchdog**: Configure `WatchdogTimerConfig` for the
   `i6300esb` QEMU watchdog. Add watchdog device to VM config:

   ```bash
   ssh root@atlas "qm set 10000 -watchdog model=i6300esb,action=reset"
   ```

   Then configure Talos to pet it via machine config patch.

5. **Configure remote kernel log forwarding**: Forward Talos kernel logs to Loki
   in real-time so NMI messages are preserved even if the node crashes.

   ```yaml
   machine:
     logging:
       destinations:
         - endpoint: "tcp://loki.monitoring.svc.cluster.local:3100"
           format: json_lines
   ```

   Chicken-and-egg: Loki runs in-cluster, so forwarding only works after Nebula is up.
   For Proxmox nodes, could forward to a host-local syslog instead.

### Long-term

6. **Investigate KVM vNMI on Zen 5**: Check for known issues with `kvm_amd.vnmi=Y`
   on Ryzen 9000 series. Consider `kvm_amd.vnmi=0` as a workaround if NMIs recur.

7. **Investigate boot hang**: The post-reboot CRI registration hang needs separate
   investigation — may be a Talos v1.12.3 bug or filesystem corruption from the NMI crash.

## Incident 3 — 2026-03-26 (RCU stall, no NMI)

### Timeline

- **2026-03-26 03:18 UTC**: VM restarted after applying `nmi_watchdog=0` on atlas.
- **2026-03-26 03:19 UTC**: Boot completed, node `Ready`. etcd rejoined (3/3 members).
- **2026-03-26 04:15 UTC**: Health check `DeadlineExceeded` for kubelet and containerd.
- **2026-03-26 04:16:21 UTC**: **RCU stall on CPU 1** — `rcu_sched detected stalls`,
  zero ticks, softirq stuck at 72283/72283. No NMI this time.
- **2026-03-26 ~06:20 UTC**: Node `NotReady`. Talos API unreachable (both nebula and VLAN).

### Key observations

- **No NMI** — `nmi_watchdog=0` was active on host, but the NMI wasn't the failure mode
  this time. Instead, a **hard RCU stall on CPU 1**.
- Same CPU (1) as both NMI incidents.
- wyrm2 (32 vCPUs, same host) has **zero NMIs** and no RCU stalls. Key config differences:
  wyrm2 uses `vga: virtio` (not QXL) and `balloon: 0` (not enabled).
- `ostype: l26` vs `other` investigated — no NMI/PMU/KVM differences.
- Talos kernel has `CONFIG_HARDLOCKUP_DETECTOR` not set, no `nmi_watchdog`.
  Go runtime uses SIGPROF, not PMU. Neither guest kernel nor Go programs are generating
  perf NMIs.
- Could not get full RCU stall stack trace — Talos API unreachable when stalled.

### Remediation applied

- **2026-03-26 06:32 UTC**: Changed VM 10000 config:
  - `vga: qxl` → `vga: virtio,memory=64` (QXL has known TTM stall bugs)
  - Added `balloon: 0` (balloon driver can cause memory management stalls)
- VM restarted. Monitoring for recurrence.

## TODOs

- [ ] Understand whether `pmu=off` and/or `nmi_watchdog=0` actually prevents vNMI
      injection into guests (read KVM source or test empirically)
- [ ] Apply `pmu=off` to talos-pve-cp-0 CPU config in `proxmox-nodes.tf`
- [ ] Consider `nmi_watchdog=0` on atlas (host kernel cmdline)
- [ ] Remove cupsd from atlas (`apt purge cups cups-daemon`)
- [ ] Add Prometheus alert for node NotReady
- [ ] Configure QEMU watchdog (`i6300esb`) + Talos `WatchdogTimerConfig`
- [ ] Configure Talos remote kernel log forwarding
- [ ] If NMIs recur after `pmu=off`: try `kvm_amd.vnmi=0` on the host
