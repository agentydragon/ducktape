# VM fixture workspace-device discovery (2026-10-31)

## Historical critical-chain probe

The passing six-scenario probe was BuildBuddy invocation
[`a394fe63-0e05-4287-8748-08edfbc2228a`](https://app.buildbuddy.io/invocation/a394fe63-0e05-4287-8748-08edfbc2228a),
from source commit `5f3c7502ee` (merged by PR #13359 as `747d69982b3725940df475b24421ff8068fc4520`).
Its OOM guest reported this systemd critical chain:

| Unit or device                        |       Start | Activation elapsed |
| ------------------------------------- | ----------: | -----------------: |
| Workspace `.device`                   |    15.319 s |   device discovery |
| `systemd-makefs@...workspace.service` |    15.942 s |            2.425 s |
| `state-workspaces.mount`              |    18.462 s |            1.063 s |
| `local-fs.target`                     |    19.547 s |                  — |
| `dbus.service`                        |    22.138 s |            0.971 s |
| `agentplane-runner.service`           | after D-Bus |            2.303 s |

The historical `blame` output included `systemd-journald.service` 7.471 s,
`systemd-udev-trigger.service` 6.751 s and `systemd-udevd.service` 2.616 s. These overlap; do not
add them to the critical-chain durations. The earlier exploratory invocation
[`892b7b7e-0e7c-46a9-b342-1ee0f2291057`](https://app.buildbuddy.io/invocation/892b7b7e-0e7c-46a9-b342-1ee0f2291057)
blocked on the pager and hit the OOM Runner's 300-second Ready deadline. Use the absolute
`/usr/bin/systemd-analyze` with `--no-pager`. The `15.319 s` is a systemd value from that earlier
boot; by itself it does not say when the kernel registered the device.

## Same-run device evidence

The first automatic collection ran as `//agentplane/system_tests/suites:test_vm`, run 1 / shard 1 /
attempt 1, BuildBuddy invocation
[`347b6930-c685-4637-9850-17eb82d9c18e`](https://app.buildbuddy.io/invocation/347b6930-c685-4637-9850-17eb82d9c18e),
test action 387.854 s. This is the pre-review `9d882959` working-tree snapshot. The six VM scenarios
passed, but its initial report ended `status=partial` because BusyBox applets were called at
nonexistent coreutils paths; the reporter also ran optional analyzers while a oneshot job held
`multi-user.target` open. The harness patch now uses explicit BusyBox applets and a bounded
`Type=exec` reporter. The first run's console artifact is
`test.outputs/vm-artifacts/consoles/systest_virt-launcher-systest-vm-codex-oom-8mk5t_9c9311e0-9c18-473e-a5d2-8f9ffbceea0c/0.log-646826.cri.log`
for OOM launcher UID `9c9311e0-9c18-473e-a5d2-8f9ffbceea0c`.

The matching QEMU launcher log configured `/dev/workspace` as a non-transitional virtio-blk device
with serial `workspace`. In this Debian kernel `6.12.63` guest (systemd `257.9`, BusyBox initramfs,
EROFS root, QEMU TCG on RBE), the kernel registered it as `virtio10` / `/dev/vde`, 10 GiB. These
are test-fixture results, not production NixOS timings.

| Guest timestamp | Observation from the same invocation                                                                                                                                                                   |
| --------------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
|         6.610 s | Kernel printk registers `virtio10` as `/dev/vde`.                                                                                                                                                      |
|        11.923 s | systemd starts waiting for `/dev/disk/by-id/virtio-workspace`.                                                                                                                                         |
| 11.960–11.962 s | systemd reports its udev control and kernel sockets listening.                                                                                                                                         |
|        14.793 s | `agentplane-boot-device-monitor.service` ActiveEnter; its unit orders it before coldplug.                                                                                                              |
|        16.052 s | `systemd-udev-trigger.service` starts.                                                                                                                                                                 |
| 20.032–23.314 s | systemd journal records `systemd-udevd` starting and started.                                                                                                                                          |
|        23.792 s | The monitor receives `KERNEL add .../virtio10/block/vde`, a coldplug event. This is not the original kernel registration time.                                                                         |
|        24.358 s | systemd journal records coldplug trigger completion.                                                                                                                                                   |
|        26.286 s | The monitor receives the matching `UDEV add`; its properties include `DEVNAME=/dev/vde`, `ID_SERIAL=workspace`, and `DEVLINKS=/dev/disk/by-id/virtio-workspace`. The alias exists by this observation. |
|        26.723 s | systemd records the workspace `.device` ActiveEnter timestamp.                                                                                                                                         |
| 27.359–30.088 s | Short-monotonic journal records makefs start, format completion and unit completion.                                                                                                                   |
|        31.088 s | Short-monotonic journal records `/state/workspaces` mounted.                                                                                                                                           |

The monitor starts before systemd coldplug and records its replay and later udev result, but it
starts after the kernel's original block-registration printk. A `KERNEL` event timestamp is a
monitor observation of the event; a `UDEV` event shows that the event and its properties reached the
monitor. Neither gives an exact physical event-emission or first-alias-creation time, nor isolates
pure udev rule execution time. The old hardware-database rebuild is already skipped on boot; its
prebuilt-image fix is documented in [PR #13011](https://github.com/agentydragon/ducktape/pull/13011).

Do not compare the 6.610-second registration directly with the historical 15.319-second
critical-chain value: they are from different invocations, and the first automatic report did not
retain manager clock-origin fields. `/proc/uptime` is guest `CLOCK_BOOTTIME` (includes suspend);
udev monitor timestamps, journal `short-monotonic`, and systemd `*TimestampMonotonic` use guest
`CLOCK_MONOTONIC`. This ordinary OOM guest did not suspend during boot. Outer CRI timestamps are
host receipt times and may be buffered.

The normal `test_vm` harness now collects block-device monitor events, workspace properties,
systemd clock origins, per-unit ExecMain timestamps, and the short-monotonic journal on its OOM
guest. Successful teardown requires the monitor-ready receipt, every required report section and a
complete report receipt across rotated logs for that launcher UID. See
[`system_tests/k3s/README.md`](../system_tests/k3s/README.md). Check the exact-head PR CI artifact
before drawing the absolute clock mapping or assigning the remaining time to kernel, udevd,
coldplug, or service execution.
