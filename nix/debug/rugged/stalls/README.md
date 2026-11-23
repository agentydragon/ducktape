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

`nix/nixos/hosts/rugged/default.nix` temporarily selected the existing
Nixpkgs-master `linuxPackages_testing` set. It evaluated to Linux 7.2-rc2 and
contained that exact Xe allocation-policy change. The 2026-08-26 follow-up below
records its removal after the Xe fix was confirmed in released Linux 7.1.8. The
no-trigger acceptance criterion in this runbook remains for the Xe/TTM incident;
it is independent of the later Cilium regression on Linux 7.2.

### A/B runbook and acceptance criteria

Before capturing, record the booted kernel:

```bash
uname -r
```

If the kernel has changed since the later follow-up below, verify its source
contains the Xe repair; the version string alone does not establish that.

During ordinary Chrome + GNOME use, leave this diagnostic-only watcher running:

```bash
sudo -E nix/debug/rugged/stalls/capture_memory_fragmentation.sh \
  --watch 14400 --duration 30
```

It records a one-second `watch.tsv`, and only on a simultaneous spike of at
least 16 MiB/s swap-out and 10 compaction stalls/s does it collect perf call
graphs, `/proc/pagetypeinfo`, VM counters, and Xe DRM debugfs client state.

Success is no trigger over a representative normal-use window. A material
partial improvement is no sustained swap I/O or meaningful high-order kswapd
wakes even if a capture occurs. Failure is a rapid trigger near the recorded
baseline with the same Xe TTM/rebind stack.

## Symptom and earlier suspects (2026-05-19 to 2026-06-11)

The desktop freezes for roughly 1-10 seconds, felt about every five minutes. The
pointer can still move; windows do not react.

- **IIO debug log flood (2026-05-19): not the only cause.**
  `ducktape.iioDebug.enable = true` (kernel dynamic debug for the HID-sensor and
  IIO modules, plus `G_MESSAGES_DEBUG=all` on `iio-sensor-proxy`) wrote 13,211
  journal lines in 10 minutes and 37.5 GB since boot, with the persistent
  journal near its 4 GB cap. That fits journald's five-minute sync. Stalls
  continued after it was off (2026-05-20). `nix/nixos/hosts/rugged/default.nix`
  keeps it off; enable it only for an active auto-rotate capture. Switching it
  off leaves runtime dynamic-debug flags until reboot, or write `module <m> -p`
  to `/sys/kernel/debug/dynamic_debug/control` for each `dyndbgModules` entry of
  `iio-debug.nix`.
- **aiquota GNOME extension (2026-05-20): fixed.** It ran
  `GLib.spawn_command_line_sync` on Shell's main thread every 120 s. A slow
  provider fetch (8.6 s measured) froze windows while the pointer kept moving.
  Fixed with an async subprocess; `aiquota/gnome/extension.js` now talks to a
  D-Bus service and spawns nothing. Rule: no synchronous subprocess on the Shell
  main thread.
- **Swap and refault (2026-06-11).** 15 GiB of swap in use with 23 GiB of RAM
  available; `vmstat` caught block reads, one 7% IO-wait sample and nonzero
  swap-in. The 2026-07-13 section above explains paging with free RAM; this
  sample was not separately tied to it. No `vm.swappiness` setting remains under
  `nix/`, and `default.nix` has `zramSwap.enable = true` only.
- **USB HID flapping (2026-06-11): untested.** The active pointer
  (`USB-HID Keyboard Mouse` on `usb 3-4.3`) reset and disconnected repeatedly
  all boot. If pointer-only lag persists, try the device on another port or hub,
  or swap the device or cable.

## Probe script

The current probe script is:

```bash
sudo -E nix/debug/rugged/stalls/probe_gnome_stalls.sh
```

Default behavior is intentionally high-information for the active RCA: 30
minutes, `0.2 s` interval, snapshots on, `gdb` thread backtraces on, live
`gcore` capture on, and a `10 s` `perf` sample on each slow trigger.

To mark a felt stall manually after it recovers:

```bash
printf '%s\t%s\n' "$(date +%s%3N)" "felt stall" \
  >> nix/debug/rugged/stalls/captures/manual_marks.tsv
```

Captured TSVs are written under `nix/debug/rugged/stalls/captures/` and are git-ignored.
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
`nix/debug/rugged/stalls/captures/snapshots/<ts_ms>/`. The default threshold is
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
sudo -E nix/debug/rugged/stalls/probe_gnome_stalls.sh
```

Lower-impact fallback:

```bash
sudo -E nix/debug/rugged/stalls/probe_gnome_stalls.sh \
  --no-attach-stacks \
  --no-gcore \
  --perf-seconds 0
```

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

### Later kernel and Cilium follow-up (2026-09-28)

During the September 5–16, 2026 interim, that file pinned `linuxPackages_7_1`, not
`linuxPackages_latest`: the alias was used here first and floated to 7.2 on
2026-09-04, which re-broke `cilium-agent` (see
`cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md`).
PR #7078 later moved the pin to the 7.2 series. After Cilium 1.19.8 was validated
on `rugged` running 7.2.7, the active config moved to `linuxPackages_latest`, which
currently resolves to that same kernel derivation. The Xe fix still sets this host's
kernel floor, so any future kernel-series change has to keep `ba7fd1634228` — verify
with the same reverse-patch test used above.
