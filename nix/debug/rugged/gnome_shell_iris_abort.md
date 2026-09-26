# GNOME Shell recurring crash → logged out (rugged)

## Symptom

Wake up / step away → graphical session gone, back at GDM greeter, GUI windows lost.
tmux still reattachable; machine never rebooted (uptime continuous). Recurs every
few days, any time of day. Example: 2026-06-27 03:46 (overnight).

## Root cause

Mesa **iris** GPU driver aborts when a GPU command-batch submission fails. The
`abort()` raises SIGABRT in gnome-shell; on Wayland the shell _is_ the compositor, so
the whole session tears down and GDM restarts the greeter. The system stays up, which
is why tmux survives.

Identical native sink across every captured core — three different GL entry points, one
fatal path:

```
abort()
_iris_batch_flush.cold()      (libgallium-25.2.6)
  ├─ Jun 27  iris_fence_flush ← st_glFlush ← create_timestamp_query ← swap_buffers (frame redraw)
  ├─ Jun 18  iris_fence_flush ← st_context_flush ← dri_create_fence_fd ← eglCreateSyncKHR
  └─ Jun 21  iris_blit ← tc_batch_execute ← tc_texture_map ← st_TexSubImage (texture upload)
```

The GJS "== Stack trace ==" context dumps in the journal (pop-shell, appindicator,
nightthemeswitcher, aiquota) are **red herrings** — ambient JS warnings, not the crash.
The fault is entirely in C.

## Environment

- GPU: Intel Lunar Lake, Arc Graphics 130V/140V (`00:02.0`, rev 04)
- Kernel DRM driver: `xe` 1.1.0 (GuC `lnl_guc_70.bin` 70.65.0) — the new Intel driver
- Userspace: Mesa iris 25.2.6 (stable channel)
- Kernel: 7.0.11

## Why it's a driver-maturity bug, not a GPU hang

No kernel-side GPU hang/reset is logged at any crash time. Only a boot-time
`xe 0000:00:02.0: [drm] Selective fetch area calculation failed in pipe A` (a PSR /
panel-self-refresh selective-fetch quirk). Userspace `abort()` with no kernel hang ⇒
iris is intolerant of some return code from the young `xe` uAPI rather than recovering
from a real reset. Reportable upstream: iris should not `abort()` on submission failure.

## Captured evidence

`coredumpctl list | grep gnome-shell` — 6 SIGABRT cores (May 25 → Jun 27); cores for
Jun 18 (4335), Jun 21 (2538388), Jun 27 (4434) decompressed and symbolicated with
`gdb` against `…gnome-shell-49.4/bin/.gnome-shell-wrapped`. Mesa is stripped, so `.cold`
does not reveal the errno iris saw — that is the missing datum (see MESA_DEBUG below).

## Trigger: suspend/resume correlation (2026-06-27)

Cross-referencing the 6 crash timestamps against the journal split them into two
populations sharing one abort sink:

- **Resume-triggered (3/6)** — the abort fires seconds after a resume from suspend.
  Smoking gun (May 25): `Finished Post-Resume Actions` at 17:47:51 → gnome-shell
  `== Stack trace ==` + SIGABRT at 17:47:52 (**1 second** later). Same for Jun 1
  (+8 s) and Jun 18 (+6 s). Consistent with the backtraces: the _first_ GPU work
  after resume (first swap / fence / timestamp-query) fails.
- **Active-use (≥3/6)** — Jun 12, 21, 27 have no resume in the prior 60 s; Jun 21
  was active Chrome use.

**Rate:** the journal holds **114 resume events** against ~3 resume-linked crashes ⇒
**~2.6 % per resume cycle**, not "1 per 6 days". This makes the resume population
reproducible on demand (see deferred harness) and gives natural observation a real
denominator: after a config change, compare `journalctl | grep -c 'system is resuming'`
against `coredumpctl list | grep -c gnome-shell` since the change. ~150 clean resume
cycles ⇒ ~98 % confidence the change helped (`0.974¹⁵⁰ ≈ 2 %` clean-by-luck).

## Mesa version gap (checked 2026-06-27)

Stable channel (`nixos-25.11`) ships **mesa 25.2.6** (Oct 2025). Latest is **26.1.3**
(2026-06-18). nixpkgs-unstable = 26.1.2, nixpkgs-master = 26.1.3.

Scanned release notes 25.2.7 → 25.3.0 for iris/LNL fixes touching _this_ bug:

- **No release note explicitly fixes "iris batch-flush abort on Lunar Lake."** No
  guaranteed silver bullet.
- Nearly all LNL fixes are **anv** (Vulkan / game crashes) — the compositor uses **iris**
  (GL), so they don't apply here.
- **Directly relevant:** 25.2.8 added _"drirc/iris: add drirc to disable threaded
  context"_. Our backtraces run through gallium **threaded context** (`tc_flush`,
  `tc_batch_execute`, `_tc_sync`, `tc_texture_map`) — TC is a plausible trigger.

## What is wired up (PR: rugged gpu-debug.nix)

`nix/nixos/hosts/rugged/gpu-debug.nix`:

1. **Mesa bump** — `hardware.graphics.package{,32}` → `nixpkgs-unstable.mesa` (26.1.2,
   Hydra-cached; master's 26.1.3 would build from source). Swaps only the driver
   package; rest of the system stays on stable.
2. **`MESA_DEBUG=1`** session-wide → iris logs the submit failure + errno before
   `abort()`. **The missing datum.** Tombstoned for removal once root-caused.
3. **Coredump retention raised** so gnome-shell cores aren't rotated out.
4. **xe devcoredump capture** — udev → `capture-devcoredump@.service` copies
   `/sys/class/devcoredump/*/data` to `/var/lib/devcoredump` before it self-expires.
5. **Active experiment — `xe.enable_psr2_sel_fetch=0`** (`boot.kernelParams`). The
   resume correlation + the boot-time `Selective fetch … pipe A` failure make PSR2
   selective fetch the targeted suspect. Disables only selective fetch (full-frame
   PSR still self-refreshes static content → ~nil battery cost). **Observing
   naturally** — no repro harness for now (user preference). Verdict method: the
   resumes-vs-crashes denominator above.

## PSR knob reference (xe, reuses i915 display)

Graduated, cheapest first:

- `xe.enable_psr2_sel_fetch=0` — disable selective fetch only (**active**). ~nil cost.
- `xe.psr_safest_params=1` — keep PSR, safe VBT values (tests "bad VBT" hypothesis).
- `xe.enable_psr=0` (+ `xe.enable_panel_replay=0`) — full PSR off. Only real-cost knob;
  still sub-watt, backlight unaffected.

## Deferred — pick up later

**Repro harness (the keystone, not yet built).** `rtcwake -s N` loop: suspend → wake →
check for a fresh gnome-shell core → log cycle count + active config → repeat. At
~2.6 %/cycle, ~100 cycles ≈ 95 % chance to reproduce in 1–2 h — collapses every
hypothesis from weeks-of-natural-observation to an afternoon, and removes the
Mesa-vs-PSR confound by holding everything else fixed and flipping one knob at a time.
Declined for now in favour of natural observation; build this if natural data is too
slow or ambiguous.

**Other levers (run through the harness if built):**

- **Capture the kernel errno (bpftrace).** All 6 crashes show only the GJS context dump
  then ABRT — no iris error string — so it is a hard `abort()`/assert, and `MESA_DEBUG`
  may not surface the cause. A probe on the `xe` exec/submit ioctl return value catches
  the exact errno iris gets before aborting.
- **Re-mine the 3 existing cores** against Mesa 25.2.6 debug symbols → resolve
  `_iris_batch_flush.cold` to the precise `iris_batch.c` line + failing return var.
- **Disable iris threaded context** — backtrace has `tc_*` frames. Clean drirc
  (`intel_disable_threaded_context`) needs Mesa ≥ 25.2.8 (so the bump is a prerequisite);
  `mesa_glthread=false` covers only the upper glthread layer.

## After the next crash

1. `journalctl _SYSTEMD_USER_UNIT=org.gnome.Shell@wayland.service` around the crash —
   with MESA_DEBUG=1, look for any iris submit error string + errno. Note whether a
   resume preceded it (`grep 'system is resuming'`).
2. `ls /var/lib/devcoredump/` for a captured kernel xe dump.
3. Update the resumes-vs-crashes denominator. If crash-free over many resume cycles →
   `enable_psr2_sel_fetch=0` was the fix; remove the MESA_DEBUG tombstone.
