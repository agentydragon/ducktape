# Atlas AMD KVM guest stalls

Atlas masked AMD Idle HLT Intercept with `clearcpuid=510` and disabled KVM halt
polling with `halt_poll_ns=0` on 2026-03-31. Both settings are managed in
[the Atlas playbook](../ansible/atlas.yaml).

## Failure signature and mitigation

In March 2026, guests on Atlas (Ryzen 9 9950X3D, host kernel `6.17.13-pve`)
reported vCPU stalls, RCU stalls, and unexpected NMIs. Talos guest
`talos-pve-cp-0` stalled in `pv_native_safe_halt` on kernel 6.18, disrupting etcd
and triggering API timeouts and operator restart cascades. Disabling halt polling
suppressed the idle reproducer; a loaded Talos guest still stalled. A loaded
Linux 6.12 guest stalled with halt polling disabled too.

After rebooting Atlas with `clearcpuid=510` and `halt_poll_ns=0`, the 6.12 test
guest booted in under 90 seconds with zero NMIs, and `talos-pve-cp-0` became
Ready with three healthy etcd members. The kernel flag masks
`X86_FEATURE_IDLE_HLT`; it is the targeted mitigation for the suspected KVM
SVM Idle HLT intercept path. The tests establish recovery with both settings
in place. The details of virtual NMI delivery remain a hypothesis.

## Current status and retirement

On 2026-11-17, Atlas was running `7.0.14-17-pve` with `clearcpuid=510` on
the effective command line and `halt_poll_ns=0` in sysfs. Its current boot
started on 2026-11-09. The host reported zero kernel oopses and warnings
through these boot counters when checked. Earlier on 2026-11-09, [Wyrm2 wedged](atlas/2026_11_09_wyrm2_wedge.md)
and recovered after Atlas rebooted. The collected evidence could not establish
the cause of that guest wedge.

Before retiring the settings, identify a host kernel fix for this failure
mode and validate idle and sustained-load guests on Atlas without the
workarounds in a maintenance window. Look for guest RCU stalls, unexpected
NMIs, and hangs. [Fedora bug 2448303](https://bugzilla.redhat.com/show_bug.cgi?id=2448303)
describes similar symptoms and listed no fixed version on 2026-11-17.
See the [KVM maintainer's Idle HLT change summary](https://lists.openwall.net/linux-kernel/2025/03/18/1506)
for the feature's upstream context.

[March reproduction results](https://github.com/agentydragon/ducktape/blob/251982469cd3c323d589dcd5f9396a0069c9b141/debug/kernel_6_18_amd_kvm_stall.md),
[the historical test matrix](https://github.com/agentydragon/ducktape/tree/251982469cd3c323d589dcd5f9396a0069c9b141/debug/kernel_6_18_amd_kvm_stall),
and [the original Talos incident timeline](https://github.com/agentydragon/ducktape/blob/251982469cd3c323d589dcd5f9396a0069c9b141/cluster/debug/pve-cp0-notready-2026-03-23/README.md)
are available at the recorded revision.
