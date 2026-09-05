# Rugged Periodic Stall Investigation

## 2026-07-13: Xe/TTM high-order allocation swap storm

### Finding

`rugged` can actively page while 16--19 GiB of RAM is free, even with
`vm.swappiness = 10`. This is not historical zram occupancy or ordinary
low-memory reclaim. The cause is a high-order allocation loop in the Intel
Xe/TTM graphics path on the booted Linux 7.1.2 kernel:

```text
Chrome GPU process / GNOME Shell
  -> Mesa Iris batch flush
  -> xe_exec_ioctl -> xe_vm_validate_rebind
  -> xe_ttm_tt_populate -> TTM high-order allocation
  -> compaction -> kswapd -> unrelated anonymous-page swapout
```

The allocation path needs physically contiguous 2--4 MiB blocks (orders 9 and
10). A large amount of free RAM does not imply that one such contiguous block
exists. When the high-order attempt fails, compaction and background reclaim
can page cold anonymous memory even though the normal memory zone is well above
its usual watermarks. `swappiness` biases the type of reclaim after that work
has begun; it is not a prohibition on this kind of reclaim.

### Captured evidence

The first privileged trace, `/tmp/rugged-memory-fragmentation-20260713-191239`,
showed Chrome's GPU process and GNOME Shell as the direct compaction callers,
with 148 order-10 and 41 order-9 kswapd wakes. Its call-graph follow-up at
`/tmp/rugged-memory-fragmentation-20260713-191725` resolved both callers through
Mesa Iris, `xe_ttm_tt_populate`, TTM, and the page allocator.

The triggered watcher capture,
`/tmp/rugged-memory-fragmentation-20260713-193606`, makes the failure mode
quantitative. It triggered immediately at 20,863 swap-out pages/s (about
81 MiB/s) and 76 compaction stalls/s. During the following 30 seconds, while
19 GiB remained available:

- `pswpout` increased by 237,732 pages (0.91 GiB) and `pswpin` by 377,261
  pages (1.44 GiB).
- `kswapd` scanned 1,767,503 pages (6.74 GiB) and reclaimed 1,557,221 pages
  (5.94 GiB).
- Compaction stalled 1,280 times: 1,151 failures and 129 successes.
- `kswapd` received 159 order-10 (4 MiB) and 47 order-9 (2 MiB) wakeups.
- `/proc/pagetypeinfo` had no free Normal-zone order-9 or order-10 blocks in
  any migratetype before or after the capture.

Chrome made 914 direct compaction attempts and GNOME Shell 662. The immediate
requesters are ordinary desktop rendering, not Bazel, containerd, kubelet, or a
cgroup memory limit. All user cgroups had `memory.high=max` and
`memory.max=max`; DAMON reclaim was idle in the original sampling.

### zram accounting

zram is RAM-backed. Its compressed physical footprint is charged as used RAM,
not included in `MemFree`; its logical contents appear in the Swap meter. Do
not add the two numbers. The current problem is the advancing `pswpin` and
`pswpout` counters, not the fact that zram retains old pages.

### Upstream correlation and test kernel

This matches the upstream Xe/TTM series
[`mm, drm/ttm, drm/xe: Avoid reclaim/eviction loops under fragmentation`](https://lore.gitlab.freedesktop.org/drm-ai-reviews/review-patch1-20260421012608.1474950-2-matthew.brost%40intel.com/t/),
which describes substantial free memory plus:

```text
kswapd -> Xe shrinker -> buffer-object eviction -> exec-ioctl rebind -> repeat
```

Its stated reproducer is Chrome WebGL. The Xe portion of the repair landed as
[`ba7fd1634228`](https://github.com/torvalds/linux/commit/ba7fd1634228),
`drm/xe: Set TTM device beneficial_order to 9 (2M)`, after Linux 7.1. The
booted 7.1.2 kernel lacks it.

`nix/nixos/hosts/rugged/default.nix` temporarily selects the existing
Nixpkgs-master `linuxPackages_testing` set. It evaluates to Linux 7.2-rc2 and
contains that exact Xe allocation-policy change. The host override has a
cleanup condition: remove it after a released `linuxPackages_latest` kernel
contains the repair and this test proves the storm remains gone.

### A/B runbook and acceptance criteria

After rebuilding/switching and rebooting, confirm the test kernel:

```bash
uname -r  # expected: 7.2-rc2
```

During ordinary Chrome + GNOME use, leave this diagnostic-only watcher running:

```bash
sudo -E debug/rugged/stalls/capture_memory_fragmentation.sh \
  --watch 14400 --duration 30
```

It records a one-second `watch.tsv`, and only on a simultaneous spike of at
least 16 MiB/s swap-out and 10 compaction stalls/s does it collect perf call
graphs, `/proc/pagetypeinfo`, VM counters, and Xe DRM debugfs client state.

Success is no trigger over a representative normal-use window. A material
partial improvement is no sustained swap I/O or meaningful high-order kswapd
wakes even if a capture occurs. Failure is a rapid trigger near the recorded
baseline with the same Xe TTM/rebind stack.

Date: 2026-05-19
Host: `rugged`

## Symptom

The desktop periodically freezes for roughly 1-10 seconds, perceived at about a
five-minute cadence, then resumes normally.

## Finding

The machine was still running the high-volume IIO debug instrumentation from the
auto-rotate investigation:

- `ducktape.iioDebug.enable = true`
- kernel dynamic debug enabled for HID sensor / IIO modules
- `G_MESSAGES_DEBUG=all` on `iio-sensor-proxy`

The live journal rate was far too high for normal operation:

- last 10 minutes: 13,211 total journal lines
- last 10 minutes: 10,492 kernel lines
- last 10 minutes: 2,079 `iio-sensor-proxy` lines
- `systemd-journald` had written 37.5 GB since boot
- persistent journals occupied 3.6 GB, near the configured 4 GB cap

The top repeated kernel messages were `hid_sensor_hub:sensor_hub_raw_event`
debug lines. The top userspace messages were `iio-sensor-proxy` debug lines for
light-sensor reads and repeated `No new data available on 'iio:device5'`.

This fits the five-minute stall cadence: journald's default sync interval is
five minutes unless overridden, so a large stream of debug messages can turn
periodic journal sync/rotation into visible UI stalls.

No recent kernel warnings, lockups, GPU resets, NVMe errors, or hung-task reports
were present in the initial snapshot.

## Fix

`nix/nixos/hosts/rugged/default.nix` now leaves the debug module imported but
sets:

```nix
ducktape.iioDebug.enable = false;
```

The module can still be re-enabled temporarily when actively capturing an IIO
wedge, but it should not stay enabled during normal use.

## Live Mitigation

To stop the current boot's log flood without rebooting:

```bash
for m in hid_sensor_trigger hid_sensor_iio_common hid_sensor_hub \
  hid_sensor_accel_3d hid_sensor_als industrialio intel_ishtp_hid \
  intel_ish_ipc; do
  printf 'module %s -p\n' "$m" | sudo tee /sys/kernel/debug/dynamic_debug/control >/dev/null
done

sudo mkdir -p /run/systemd/system/iio-sensor-proxy.service.d
printf '[Service]\nUnsetEnvironment=G_MESSAGES_DEBUG\n' \
  | sudo tee /run/systemd/system/iio-sensor-proxy.service.d/99-no-debug.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart iio-sensor-proxy.service
```

Then verify the rate dropped:

```bash
journalctl -k --since '1 min ago' --no-pager -q | wc -l
journalctl -u iio-sensor-proxy.service --since '1 min ago' --no-pager -q | wc -l
systemctl show iio-sensor-proxy.service -p Environment --no-pager
```

## Follow-up

After switching the NixOS config, rebooting is the cleanest way to guarantee all
boot-time dynamic-debug settings are gone. If avoiding a reboot, run the live
mitigation above after `nixos-rebuild switch`.

## Post-switch Validation

After `nixos-rebuild switch` on 2026-05-19:

- `iio-debug-watchdog.timer`, `iio-debug-watchdog.service`, and
  `iio-debug-dyndbg.service` were `not-found` and inactive.
- `systemctl show iio-sensor-proxy.service -p Environment` returned an empty
  environment, so `G_MESSAGES_DEBUG=all` was no longer active.
- The current unit came directly from the upstream `iio-sensor-proxy` service;
  its packaged `G_MESSAGES_DEBUG=all` line is commented out.
- Journal rate dropped to:
  - `kernel_lines_60s=3`
  - `iio_lines_60s=0`

A temporary `/run/systemd/system/iio-sensor-proxy.service.d/99-no-debug.conf`
drop-in was still present from the live mitigation. It is harmless and will
disappear on reboot.

## 2026-05-20 Follow-up: Stalls Still Observed

The IIO debug-log hypothesis is no longer active on the current boot:

- current system: `/nix/store/jc63ryg2kwrx4hk9hy3klbzlzn7brymg-nixos-system-rugged-25.11.20260425.a4bf066`
- `iio-debug-watchdog.timer`, `iio-debug-watchdog.service`, and
  `iio-debug-dyndbg.service`: inactive
- `iio-sensor-proxy.service`: active with empty `Environment=`
- recent journal rate:
  - `kernel_lines_60s=0`
  - `iio_lines_60s=0`
  - `iio_lines_10m=0`

The current symptom is more specific: the pointer can still move, but windows
do not react for roughly 1-10 seconds. That points away from a whole-kernel
freeze and toward the compositor/session path or severe client scheduling
pressure.

During a 3-minute ad-hoc capture on 2026-05-20, two samples hit the 5-second
GNOME Shell DBus call timeout while the session bus stayed responsive:

- `2026-05-20 14:24:45.465 PDT`: shell call `5009 ms`, session bus `17 ms`
- `2026-05-20 14:24:50.801 PDT`: shell call `5011 ms`, session bus `22 ms`

At the same time the machine had several concurrent interactive/build
workloads: multiple Codex wrappers, three Bazel JVMs, Chrome, Tana, and
transient `nix`/`nix-daemon` CPU/IO bursts. Earlier snapshots showed load in
the 4-7 range, CPU PSI around 5-11%, IO full PSI up to about 1-2%, and several
GiB of swap in use. This can make the desktop feel normal between stalls while
still creating short hard pauses when GNOME Shell or related clients miss a
scheduling window.

The current probe script is:

```bash
sudo -E debug/rugged/stalls/probe_gnome_stalls.sh
```

Default behavior is intentionally high-information for the active RCA: 30
minutes, `0.2 s` interval, snapshots on, `gdb` thread backtraces on, live
`gcore` capture on, and a `10 s` `perf` sample on each slow trigger.

To mark a felt stall manually after it recovers:

```bash
printf '%s\t%s\n' "$(date +%s%3N)" "felt stall" \
  >> debug/rugged/stalls/captures/manual_marks.tsv
```

Captured TSVs are written under `debug/rugged/stalls/captures/` and are git-ignored.
The important fields are:

- `shell_ms` / `shell_ok`: direct GNOME Shell DBus responsiveness
- `bus_ms` / `bus_ok`: session bus baseline responsiveness
- `cpu_some10`, `io_full10`, `mem_full10`: kernel PSI pressure at capture time
- `top`: top CPU consumers by process name only, without command lines

Interpretation:

- high `shell_ms` with low `bus_ms`: GNOME Shell/compositor path stalled while
  the session bus remained alive
- high `shell_ms` and high `bus_ms`: broader session bus or user-session stall
- high PSI during either case: resource pressure is likely contributing

When a `shell_ms` or `bus_ms` call is still pending at the slow threshold, the
script writes an in-flight snapshot under
`debug/rugged/stalls/captures/snapshots/<ts_ms>/`. The default threshold is
`500 ms` with a `5 s` snapshot cooldown.

The default snapshot is intentionally invasive during the current RCA pass: it
attaches `gdb`, records `perf`, and attempts a live `gcore` of GNOME Shell. This
can itself pause Shell and create follow-on slow samples after the first trigger,
so interpret repeated slow rows after the first snapshot as potentially
probe-induced. It also captures:

- trigger timing and PSI/load/top-process summary
- `/proc/pressure/*`, `/proc/loadavg`, selected `/proc/meminfo`, and
  `/proc/vmstat`
- top process/thread scheduler state by command name, not full command line
- short `vmstat`, `iostat`, and `pidstat` samples when available
- GNOME Shell PID plus `/proc/<pid>/status`, `sched`, `schedstat`, `io`,
  `smaps_rollup`, `wchan`, and per-thread `wchan`/CPU counters
- recent GNOME Shell journal lines and warning-or-higher system/user journal
  windows

Next forensic escalation if the snapshots prove GNOME Shell is the paused
component:

1. Map the hot or blocked GNOME Shell thread from `gnome-shell-threads.tsv` to
   its role by inspecting `threads-by-cpu.txt`, `wchan`, and journal context.
2. If the stalled thread is CPU-bound, run a short targeted profiler
   (`perf record -g -p <gnome-shell-pid> -- sleep 10`) during a reproducible
   window.
3. If the stalled thread is blocked in kernel wait, inspect the corresponding
   `wchan` and kernel stack. If normal-user `/proc/<pid>/stack` is permission
   limited, repeat the same snapshot with root.
4. If thread snapshots are inconclusive and Shell is still clearly stuck,
   inspect the `gdb`, `perf`, and `gcore` artifacts. The live core is large,
   privacy-sensitive, and pauses GNOME Shell while dumping.
5. If the evidence points to a GNOME Shell extension, bisect extensions in a
   temporary session before changing the normal desktop setup.

Full capture command:

```bash
sudo -E debug/rugged/stalls/probe_gnome_stalls.sh
```

Lower-impact fallback:

```bash
sudo -E debug/rugged/stalls/probe_gnome_stalls.sh \
  --no-attach-stacks \
  --no-gcore \
  --perf-seconds 0
```

## 2026-05-20 RCA: Synchronous aiquota Refresh Blocks GNOME Shell

The 18:51-19:24 capture caught the active stall pattern:

- capture: `debug/rugged/stalls/captures/20260520-185405.tsv`
- rows: 3,429
- slow rows: 46
- first true slow event: `2026-05-20 19:18:04.564 PDT`
- first event details: Shell DBus call `80 ms`, session bus call `5040 ms`
  and failed, while CPU PSI was only `2.26`, IO full PSI `0.04`, and memory
  full PSI `0.24`
- the next sample at `19:18:10.047 PDT` showed Shell DBus `4679 ms`

The low PSI and clean kernel warning window rule out the earlier journald/IIO
log-flood root cause for this capture. The later slow rows are partly polluted
by the probe itself: after the first trigger, the script started `gdb`, `gcore`,
and `perf` snapshots against GNOME Shell.

The smoking gun is the installed GNOME extension:

```text
aiquota@allegedly.works
Path: /etc/profiles/per-user/agentydragon/share/gnome-shell/extensions/aiquota@allegedly.works
State: ACTIVE
```

The installed artifact still runs:

```js
GLib.spawn_command_line_sync(`${binPath} gnome-extension-json`);
```

from the GNOME Shell extension refresh path. That executes on Shell's main
thread every 120 seconds. When network is slow or a provider times out, the
Python CLI can take several seconds; a live manual check took `8.567 s`, and
the cache at the time showed a Codex read timeout. That exactly matches the
observed symptom: pointer motion can continue, but Shell/window interaction is
stuck until the synchronous subprocess returns.

The perf snapshot also showed GNOME Shell doing normal compositor work around
the stall, including Mutter/Wayland damage processing and Intel `xe` GEM
allocation stacks:

- `meta_wayland_buffer_process_damage`
- `_mesa_TexSubImage2D`
- `xe_gem_create_ioctl`

Those stacks explain visible compositor work during recovery but are not the
root cause by themselves. The root cause is the extension blocking Shell's main
thread with a synchronous subprocess.

Repo fix: `aiquota/gnome/extension.js` now uses
`Gio.Subprocess.communicate_utf8_async()` and refuses overlapping refreshes.
That keeps slow quota/network fetches off the Shell main loop.

Verification status:

- `node --check aiquota/gnome/extension.js`: passed
- `bbr test //aiquota/gnome:test_render`: blocked because local `devel` has
  unpushed/diverged commits (`HEAD=4d50d46a`, `origin/devel=4b1beec0`)

Deployment note: rugged currently uses the CI-released pinned Nix artifact, not
the local source tree. The fix needs either a temporary user-local extension
install or a normal release + artifact pin sync before `home-manager switch`
will install it declaratively.

## 2026-06-11 Follow-up: Current Choppiness Is Not the IIO Log Flood

The old custom-Nix IIO log flood is not active on the current boot:

- `ducktape.iioDebug.enable = false` in
  `nix/nixos/hosts/rugged/default.nix`
- `iio-debug-watchdog.timer`, `iio-debug-watchdog.service`, and
  `iio-debug-dyndbg.service`: not found
- `iio-sensor-proxy.service` has empty `Environment=`
- recent live rate: 92 total journal lines in 10 minutes, 0 kernel lines in
  10 minutes

The May 20 aiquota GNOME Shell blocker is also not the current installed
failure mode: `aiquota@allegedly.works` is enabled, but the installed extension
uses `Gio.Subprocess.communicate_utf8_async()` rather than
`GLib.spawn_command_line_sync()`. A manual `aiquota gnome-extension-json` run
completed in about 1.5 seconds and no longer blocks the Shell main loop.

Current evidence points at two remaining contributors:

- Disk swap/refault stalls: `free -h` showed 15 GiB used swap while only
  7.7 GiB RAM was used and 23 GiB was available. `swapon --show --bytes`
  listed only `/swap/swapfile`; `zramctl` returned nothing; current
  `vm.swappiness` was 60. A short `vmstat -SM 1 8` sample caught block reads
  above 330 GiB/s reported by vmstat units and one sample at 7% IO wait, while
  swap-in was still nonzero.
- USB HID flapping: the active pointer is
  `/dev/input/by-path/pci-0000:00:14.0-usb-0:4.3:1.2-event-mouse`, i.e.
  `USB-HID Keyboard Mouse` on `usb-0000:00:14.0-4.3/input2`. Kernel logs show
  repeated resets/disconnects for that exact `usb 3-4.3` composite HID path
  throughout this boot, including today.

Repo mitigation added in `nix/nixos/hosts/rugged/default.nix`:

```nix
zramSwap = {
  enable = true;
  algorithm = "zstd";
  memoryPercent = 50;
  priority = 100;
};
boot.kernel.sysctl."vm.swappiness" = 10;
```

This keeps the existing disk swapfile as a lower-priority fallback while making
future swap activity prefer compressed RAM and reducing eager disk swap-out of
cold desktop pages. It does not fix the USB HID reset path; if pointer-only
lag continues after the memory change, test the same mouse/dongle through a
different port/hub or swap the device to isolate hardware/cable/dock flapping.

## 2026-08-26: Xe/TTM fix confirmed backported to stable, RC kernel dropped

The `linuxPackages_testing` (nixpkgs-master, 7.2-rc) pin taken for the Xe/TTM
fix (`ba7fd1634228`, "drm/xe: Set TTM device beneficial_order to 9 (2M)")
broke `cilium-agent` on `rugged` for unrelated reasons (kernel BPF verifier
hardening in the same RC tree; see
`cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md`).
Building the actual `linux-7.1.8` derivation (`pkgs.linuxPackages_latest` at
the current nixpkgs-26.05 pin) and attempting to apply that same commit as a
local patch confirmed it via `patch`'s own "reversed (or previously applied)
patch detected" — the fix is already in stable 7.1.8. `./ipu7-camera.nix` now
carries the kernel choice with no override and no local patch.

That file pins `linuxPackages_7_1`, not `linuxPackages_latest`: the alias was used
here first and floated to 7.2 on 2026-09-04, which re-broke `cilium-agent` (see
`cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md`).
The Xe fix is what sets this host's kernel floor, so any future change to that pin
has to keep `ba7fd1634228` — verify with the same reverse-patch test used above.
