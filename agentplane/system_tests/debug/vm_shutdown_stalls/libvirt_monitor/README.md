# Opt-in KubeVirt libvirt monitor logging

For an investigation run, set `AGENTPLANE_VM_LIBVIRT_MONITOR_DEBUG=1` through
`--test_env`. This adds KubeVirt's `debugLogs` label and the
`kubevirt.io/libvirt-log-filters` annotation `1:qemu.qemu_monitor 3:*` to each VM
template in that test action. It does not change the retained KubeVirt baseline or the
shared cluster. The default is off. For example:

```bash
bbr test //agentplane/system_tests/suites:test_vm \
  --test_env=AGENTPLANE_VM_LIBVIRT_MONITOR_DEBUG=1 \
  --nocache_test_results --test_output=errors
```

KubeVirt v1.8.2's [launcher template](https://github.com/kubevirt/kubevirt/blob/v1.8.2/pkg/virt-controller/services/template.go)
passes the annotation to virt-launcher and uses the label to enable libvirt debug logging.
The [KubeVirt guide](https://kubevirt.io/user-guide/debug_virt_stack/logging/) describes
per-VMI libvirt filters. The logs may be large and can perturb scheduling; do not mix these
runs with uninstrumented controls when estimating the flake rate. They can include QMP
arguments and host paths, so handle retained artifacts as test logs.

## Healthy control

[One real KubeVirt guest with this setting](https://app.buildbuddy.io/invocation/cac315ab-1f8b-4d08-b098-4169065f3ca9)
passed in 328.6 seconds on base `9b5b98113f` plus the one-guest diagnostic and this
change. Its [shutdown report](healthy-shutdown-report.json.gz) records ordered request,
proof and guest powerdown, and the UID-correlated handler reported `Shutoff reason
Shutdown`. The complete retained [launcher log](healthy-launcher.log.gz) decompresses
to SHA-256 `c2e15f249f4cc2fc833d6f6ffbff2844fb6ffa33bcbc15c6b3d871f257fe1a9c`
(4,854,846 bytes). It contains 2,126 libvirt entries, including QMP
`system_powerdown` commands at 22:42:14 UTC and a `SHUTDOWN` event with
`guest: true` at 22:42:22.102. A later `SHUTDOWN` event with `reason: host-signal`
also appears after the guest-originated shutdown in this healthy run; that event alone
is not proof that KubeVirt forcibly killed a stalled guest.

This validates collection, not a repair or reproduction of the intermittent stall.
