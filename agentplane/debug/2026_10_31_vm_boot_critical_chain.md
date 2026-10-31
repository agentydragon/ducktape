# VM fixture boot critical chain (2026-10-31)

The VM system test runs a Debian 6.12 kernel, busybox initramfs, EROFS root, and systemd under
QEMU TCG on the BuildBuddy executor. All six retained launchers in the earlier audit used
`-accel tcg`; the RBE host has no `/dev/kvm` or `CONFIG_KVM`. These timings describe the test
fixture, not production's NixOS guest.

## Boot ordering

The [focused VM probe](https://app.buildbuddy.io/invocation/a394fe63-0e05-4287-8748-08edfbc2228a)
passed all six scenarios (test action 308.5 s). The OOM guest's retained serial console contains
`/usr/bin/systemd-analyze --no-pager critical-chain agentplane-runner.service` and `blame`.
Its critical chain, in systemd's guest boot time, was:

| Unit or device                        |       Start | Activation elapsed |
| ------------------------------------- | ----------: | -----------------: |
| Workspace block device                |    15.319 s |   device discovery |
| `systemd-makefs@...workspace.service` |    15.942 s |            2.425 s |
| `state-workspaces.mount`              |    18.462 s |            1.063 s |
| `local-fs.target`                     |    19.547 s |                  — |
| `dbus.service`                        |    22.138 s |            0.971 s |
| `agentplane-runner.service`           | after D-Bus |            2.303 s |

`blame` separately reports `systemd-journald.service` 7.471 s,
`systemd-udev-trigger.service` 6.751 s, and `systemd-udevd.service` 2.616 s. Those units overlap;
their durations do not add to the critical chain. The blocking path enters systemd through
workspace device discovery at 15.319 s, then filesystem creation and mount. The console's first
kernel lines and systemd timestamps are different clocks, so this chain does not attribute the
entire kernel-to-workspace interval to one unit or measure its CPU use.

The first exploratory [probe run](https://app.buildbuddy.io/invocation/892b7b7e-0e7c-46a9-b342-1ee0f2291057)
failed because `systemd-analyze` started a pager on the serial console and blocked the OOM Runner
until its 300 s Ready deadline. Explicit `--no-pager` fixed that failure; the passing probe above
establishes the corrected behavior. The absolute `/usr/bin/systemd-analyze` path exists in the
guest image.

Repeat with:

```sh
bbr test //agentplane/system_tests/suites:test_vm --noremote_accept_cached \
  --test_env=AGENTPLANE_VM_BOOT_PROBE=1
```

The running fixture adds the kernel argument only to the OOM SandboxTemplate. Its serial output is retained under
`test.outputs/vm-artifacts/consoles/` in the BuildBuddy test artifacts; search that guest's log for
`AGENTPLANE_BOOT_CRITICAL_CHAIN_BEGIN` and `AGENTPLANE_BOOT_BLAME_BEGIN`.
