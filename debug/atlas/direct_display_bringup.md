# Direct Display Bring-up (5090 → FV43U DP)

Running notes for the plan in <gpu-strategy.md> "Plan: direct display output".
Goal: games on wyrm2 render **and scan out** on a 5090 plugged straight into
the monitor's DP input; desktop stays on SPICE/virtio; keyboard reaches wyrm2
via the FV43U's USB-B hub uplink → atlas USB port → QEMU port passthrough.

## State (2026-07-02 ~23:00)

- Video path GPU→cable→panel: **verified working** (monitor locks 3840×2160
  on DP) — but shows an all-black image (compositor issue, see below).
- USB-B input path: **not yet working**, cable suspect (see below).
- wyrm2: `modeset=1` active (reverted from the brief `=0` experiment),
  Sunshine deployed (x264-only), SPICE auto-resize working.

## Timeline / findings (2026-07-02 evening)

1. **Wired**: Ivanky 8K DP m-m: 5090 DP-OUT → FV43U DP 1.4 in. Spare USB A→B:
   FV43U USB-B uplink → atlas rear USB-A.
2. **USB-B, first atlas port**: repeating
   `usb usb4-port4: Cannot enable. Maybe the USB cable is bad?` (~every 4s).
   Moved to neighbouring port → no more errors, but also no enumeration —
   inconclusive because the monitor never routed its hub to USB-B (below).
3. **FV43U KVM behavior**: KVM Wizard bindings set (USB-B ↔ DP,
   Type-C ↔ Type-C). KVM Switch with a dark DP input → few seconds of black,
   then **auto-reverts to USB-C on its own**; during the attempt the hub
   never left the USB-C uplink (zero USB events on atlas). USB half appears
   gated on the target input having signal. Video-only input switch is via
   OSD Input menu — use that to isolate video from USB when testing.
4. **`modeset=1` reboot**: `card0-DP-1` = connected, 3840×2160, `enabled`,
   DPMS On. Monitor **locks 4K signal but displays all-black** on DP.
   Meanwhile SPICE (virtio) showed the GDM greeter's _secondary_ expanse:
   grey + cursor with weird coordinate mapping, no login UI — greeter put
   its UI on the new "primary" (DP) output, which is black. User stuck: DP
   black, SPICE has no login box → dropped to VT.
5. **Hypothesis for the black DP output**: mutter renders the greeter on one
   GPU and uses its secondary-GPU copy path for the DP output; the copy
   produces black frames (cross-GPU copy with virgl-primary +
   nvidia-open-secondary is an unusual combo). Signal itself is healthy.

6. **Login unblocked** (~23:05): unplugging DP made the greeter re-layout
   onto SPICE; normal login worked. Confirms the greeter/session is healthy
   and the problem is specifically the mutter copy path to the NVIDIA
   output.

## Next steps

- [x] Unblock login: unplug DP (greeter re-layouts onto SPICE) → log in →
      replug. Worked.
- [x] **FAILED + reverted**: udev tag `mutter-device-preferred-primary` on
      the display 5090. The tag mechanism works (journal: "GPU
      /dev/dri/card2 selected primary given udev rule") but **gnome-shell
      49.4 SIGSEGV-crash-loops** (~every 5s, `coredumpctl` full of
      `.gnome-shell-wrapped` SIGSEGV; journal frames in libmutter
      KMS/thread*impl code) with nvidia-primary + virtio-secondary.
      Symptom from the user seat: DP mostly black with a ~0.3s grey flash
      every ~5s (each greeter restart), SPICE showing a bare VT.
      Note: on the DP replug the cable landed on the \_other* 5090 than
      before (card2/DP-4/02:00.0 vs card0/DP-1/01:00.0 pre-reboot) — the
      two GPUs are physically distinguishable this way.

## Revised approach after mutter crash: gamescope kiosk on the NVIDIA card

Mutter can't be primary on NVIDIA (crashes) and can't copy to NVIDIA
(black), so take mutter out of the NVIDIA-scanout business entirely:

- Tag both NVIDIA cards `mutter-device-ignore` → mutter manages only
  virtio; desktop/SPICE guaranteed to keep working (and greeter layout
  stops jumping to the DP output).
- Run games in **gamescope with the DRM backend directly on the display
  5090** (its own KMS master, no compositor in between), launched on demand
  (systemd unit or from the desktop). Steam Big Picture inside gamescope.
- Render nodes are unaffected by `mutter-device-ignore` — CUDA/NVENC/PRIME
  offload for desktop apps keep working.

- [x] Reverted — **but required a VM reboot, not just rule removal**.
      **Gotcha (udev tag persistence)**: removing the rule +
      `udevadm trigger --action=change` clears `CURRENT_TAGS` but NOT the
      persistent `TAGS` set in the udev db, and mutter's
      `udev_device_has_tag()` reads the persistent set — so gnome-shell
      kept crash-looping after the "revert" until reboot rebuilt the udev
      db. Desktop confirmed healthy post-reboot (2026-07-02 23:12, DP cable
      unplugged for now).

### Design (decided 2026-07-02 late)

- **Behind a login** (user requirement): the game display is a real logind
  **second seat** (`seat-game`) with a greeter — nothing runs on the 5090
  until someone authenticates at the monitor.
- **Same user** (agentydragon) logs in there — shared Steam library/home;
  concurrent with the seat0 SPICE session (gamescope session, not a second
  GNOME, so shared-state risk is low). Dedicated `games` user is a cheap
  refactor later if wanted.
- DRM master is per-device → SPICE desktop (card1) and gamescope (card2)
  coexist by construction. Non-seat0 seats have no VTs → both sessions
  always active, no VT fights.
- **Greeter: decision ladder** (revised — user would also like the option
  of a full GNOME session on the GPU seat):
  1. Try **GDM multiseat** first — GDM speaks multiseat natively and the
     seat-filtered greeter would be _single-GPU NVIDIA_ mutter (mainstream
     path, different from the 3-card crash). If the greeter renders, the
     session picker can offer both GNOME and a gamescope/Steam session.
  2. If mutter crashes on the seat → **greetd → cage (wlroots kiosk) →
     gtkgreet**, gamescope-only. Non-seat0 seats have no VTs → text
     greeters are out. NixOS `services.greetd` is single-instance →
     seat-game greetd would be a small custom unit with
     `XDG_SEAT=seat-game`.
  - **Constraint**: GNOME can't run twice for one user (fixed D-Bus names
    on the user bus; GDM redirects a same-user second login). Concurrent
    GNOME-on-seat-game next to the SPICE desktop needs a separate `games`
    user. The gamescope session is same-user-safe (but Steam is
    single-instance per home — desktop Steam and seat Steam can't run
    simultaneously).
- Monitor dual-KVM maps 1:1: USB-C input+uplink ↔ seat0/Sabrent-KVM world;
  DP input + USB-B uplink ↔ seat-game world.

### Build list

- [x] **Wired in config (2026-07-02 late)**, pending replug + reboot +
      test: `ID_SEAT=seat-game` on the display 5090 (guest `02:00.0`),
      `mutter-device-ignore` on the compute 5090 (`01:00.0`) — note the
      display card gets seat assignment INSTEAD of an ignore tag (the
      seat-game greeter must use it; seat filtering keeps seat0's mutter
      off it). Plus `programs.steam.gamescopeSession.enable` for the
      "Steam (gamescope)" entry in GDM's session picker.
      **Test criterion after replug (same GPU as before — the one that
      enumerated as card2/DP-4) + rebuild + reboot**: `loginctl
list-seats` shows `seat-game`; a GDM greeter renders on the DP
      input (mutter single-GPU-NVIDIA test); SPICE desktop unaffected.
      Greeter will be **display-only** until the seat has input devices
      (USB-B still unpinned).
- [ ] udev `ID_SEAT` for the USB-B passthrough port devices → `seat-game`
      (after the port pin below).
- [ ] `qm set 110 --usb3 host=<bus>-<port>,usb3=1` once the USB-B/KVM
      bench test identifies the atlas port (cable still unverified).
- [ ] GDM-greeter-on-seat test (free once the seat exists — GDM already
      installed). Renders → keep GDM; offer GNOME (as `games` user) +
      gamescope/Steam (as agentydragon) sessions. Crashes → greetd + cage + gtkgreet fallback, gamescope-only.
- [ ] Gamescope session entry: `gamescope --backend drm -- steam
-gamepadui` (same user, agentydragon).
- [ ] Audio: GPU DP/HDMI audio functions (`.1`) are NOT passed through
      (hostpci passes `01:00.0`/`03:00.0` only) — decide: pass audio
      function for monitor-jack audio vs. keep audio on the SPICE path.
- [ ] Once DP shows a live desktop: retry FV43U KVM Switch — with DP signal
      present the USB half should engage; watch atlas dmesg for where the
      monitor hub enumerates; distinguish "bad USB A→B cable" (link errors)
      from "monitor gating" (no events).
- [ ] Pin that atlas USB port to VM 110: `qm set 110 --usb3 host=<bus>-<port>,usb3=1`
      (`hotplug: usb` already on; usb0=spice, usb1/usb2 taken).
- [ ] Then: PRIME offload test (`__NV_PRIME_RENDER_OFFLOAD=1
__GLX_VENDOR_LIBRARY_NAME=nvidia glxinfo -B`), Stellaris via Steam
      launch options, fullscreen on the DP output.
- [ ] Watch: does GNOME write `monitors.xml` for the two-display layout, and
      does SPICE auto-resize survive (<spice_autoresize.md>)?

## 2026-07-03 (~00:00–01:00): input + session forensics → STEAM RUNNING

Working state reached: GDM greeter on the DP output (single-GPU NVIDIA
mutter works fine when the seat shows it one card), keyboard via monitor
USB-B → `qm` port-path passthrough, login → gamescope → **Steam Big
Picture on the 5090** (slightly glitchy, triage pending). Chain of
root-caused problems, in order:

1. **Seat input assignment needs the PARENT input device**: logind
   resolves an evdev node's seat via its parent `input` class device, not
   the `event*` node. `SUBSYSTEM=="input", KERNEL=="event*"` rules make
   libinput claim the device while logind denies TakeDevice (EPERM). Rule
   must match all `SUBSYSTEM=="input"` devices. Also: seat rules must run
   at **priority 72** (before `73-seat-late.rules`), like loginctl-attach
   writes them — 99-local is too late.
2. **udev persistent-TAGS gotcha** (repeat offender): removed rules leave
   tags in the udev db until reboot; `--action=change` triggers clear only
   `CURRENT_TAGS`, and mutter checks the persistent set.
3. **nix store corruption** (suspect: attic cache truncation):
   `libSDL2...so: file too short` made gamescope die at the loader —
   silently, because GDM captures no output from failed session Execs
   (60s "never registered" timeout → thrown back to greeter, zero
   journal lines). Fixed with `nix-store --repair-path`; full
   `--verify --check-contents --repair` found more corrupted links.
   **Debug pattern that cracked it**: a wrapper session .desktop whose
   Exec logs `set -x` + env + output to /tmp/steam-session.log.
4. **gamescope picks the wrong twin GPU**: gamescope derives its KMS node
   from its _Vulkan_ compositing device (`vulkan_primary_dev_id` →
   `drmGetDeviceFromDevId`), ignores `WLR_DRM_DEVICES`, and takes the
   first suitable Vulkan device — the seat0-owned compute 5090 → logind
   TakeDevice EPERM → "Could not open KMS device" → segfault. **Fix**
   (quirk exploitation, see `src/rendervulkan.cpp` in gamescope 3.16.17):
   `--prefer-vk-device 10de:2b85` matches BOTH twins and the _last_ match
   wins; Vulkan enumerates in PCI order → selects 02:00.0 = display GPU.
   Fragile if enumeration order ever changes — fails safe (crash, not
   wrong output). Mesa's `MESA_VK_DEVICE_SELECT=pci:...` did NOT work
   here (no reorder observed).
5. **Community wisdom** (validated the GDM pain): Jovian-NixOS ships
   gamescope sessions with SDDM and documents GDM as problematic for
   custom sessions. SDDM remains the escape hatch if GDM misbehaves more.

### Cleanup checklist (after glitch triage)

- [ ] Remove "Steam (debug)" session + revert `gdm.debug` in wyrm2 config
- [ ] Decide on greeter animations-off dconf (was added mid-debug; the
      original eternal-freeze-at-transition was never re-tested with
      animations on — possibly unnecessary now, possibly load-bearing)
- [ ] ~~Flicker/corruption/wrong-colors: NVIDIA direct scan-out; fixed by
      `composite_force 1`.~~ **CORRECTION (2026-07-04): this was never actually
      verified fixed.** `--force-composition` is pinned in the args but the
      corruption persists. Re-triaged live (see 2026-07-04 section): it is the
      Steam Big Picture **button-hint bar only** — a Steam-side bug (#11843),
      NOT scanout/composition. `composite_force 1`, `drm_debug_disable_explicit_sync 1`,
      and `wayland_use_modifiers 0` all had zero effect. HDR: not pursued.
- [x] ~~Lag remains (UI sluggish even composited).~~ **Root-caused 2026-07-04:**
      it was not gamescope lag — Steam ran its web helper with `--disable-gpu`
      (Valve NVIDIA default-off bug #13151), so the Big Picture CEF UI
      rasterized on CPU (~5 FPS at 4K). Fixed by enabling `GPUAccelWebViewsV3`
      (see 2026-07-04 section). `capSysNice` is on and applies (nice -20) but
      was not the fix. `ValidRefreshRates: 60` is a real 4K60 link limit but
      unrelated to the choppiness.
- [ ] Sunshine now logs "Couldn't open DRM FD for CUDA device" for card2
      (seat-game owns it) — harmless, captures virtio; silence eventually
- [ ] Commit everything; update gpu-strategy.md status

## 2026-07-04: gamescope crash fix + Steam UI perf + corruption triage

Picked the seat back up after it had been unused for a day; login froze on the
greeter after password. Findings, in order:

1. **gamescope 3.16.17 SIGABRT on every launch** (looked like a greeter freeze —
   GDM waits ~60s for the dead session then bounces to the greeter). Assertion
   `wlserver_is_lock_held()` in `wlserver_mousemotion` — a seat input event
   racing `wlserver_init()` before the lock is held (upstream #1746). Fixed in
   gamescope **3.16.24** by PR #2023. nixpkgs 25.11 is pinned at 3.16.17, so
   pulled 3.16.24 from the `nixpkgs-unstable` input via an overlay in
   `nix/nixos/hosts/wyrm2/default.nix` (**PR #2879**). Assertion gone; the
   greeter animation-stall fix (`enable-animations=false`) was fine all along.

2. **wyrm2 couldn't build from devel at all** — unrelated latent breakage:
   `secrets/home/wyrm2/zai.yaml` was dropped by #2792 (moving to a LiteLLM
   virtual key) and never re-committed, while `nix/home/hosts/wyrm2.nix` still
   references it. Restored the original ciphertext from `5d3ebfef0^` (recipients
   identical, no re-key) (**PR #2882**).

3. **Session-teardown leak → re-login collisions.** A prior seat-game login
   left the whole Steam + gamescope process tree alive (session stuck
   `closing`), pinning `/run/user/1001/gamescope-0.lock` and `/tmp/.X11-unix/X0`.
   The next login spawns a _second_ gamescope that can't take `gamescope-0`/`X0`
   (falls back to `gamescope-1`/`:1`), its Steam child exits (single-instance per
   home), "Primary child shut down" → SIGSEGV → bounced to greeter. Manual
   recovery: `loginctl terminate-session <n>` + `pkill -9 gamescope/steam` +
   `rm gamescope-0.lock /tmp/.X11-unix/X0` + restart `display-manager`. **Not yet
   hardened** — every logout leaks and breaks the next login until reaped.

4. **Big Picture UI ~5 FPS scroll = Steam web helper on CPU.** Steam launched
   `steamwebhelper --disable-gpu` (confirmed in `webhelper_gpu.txt`:
   `Disabling GPU acceleration: Disabled/CommandLine`), so the CEF Big Picture UI
   rasterized on the CPU — brutal at 4K. Cause: Valve defaults "GPU accelerated
   rendering in web views" OFF on NVIDIA+Wayland (driver-detection bug #13151).
   Fix: set registry key **`GPUAccelWebViewsV3=1`** in `~/.steam/registry.vdf`
   (found by `strings` on `steamui.so`; lives in `HKCU/Software/Valve/Steam`).
   Result: UI "much much smoother". The setting is the in-Steam toggle
   (Settings → Interface); a home-manager activation
   (`home.activation.steamGpuWebViews`) only **warns** when it is not enabled —
   it deliberately does not edit `registry.vdf`, which Steam owns and rewrites.

5. **Garbled/corrupted blocks — Steam bug, not gamescope.** Confined to the Big
   Picture **button-hint footer bar**; never in the GDM greeter (mutter, same
   5090); present under both software and GPU CEF. Unaffected by
   `composite_force 1`, `drm_debug_disable_explicit_sync 1`, `wayland_use_modifiers 0`.
   → Steam-side rendering bug on NVIDIA/Wayland (#11843). Cosmetic, out of our
   control (gamescope/nix); a Steam Beta build may fix it. **This corrects the
   earlier claim that `composite_force` fixed on-screen corruption — it did not.**

6. **Stellaris (native Linux build) crashes at launch:**
   `mkdir: error while loading shared libraries: libselinux.so.1: cannot open
shared object file` — the `libselinux` in `programs.steam.extraPackages`
   doesn't reach the launcher inside the Steam Linux Runtime sandbox. Per the
   config's own note: **force Proton** on it (Properties → Compatibility).

### Live gamescope tuning (gamescopectl)

`gamescopectl` is set-only (bare invocation prints display info; reading a convar
back returns nothing). Useful convars probed live: `composite_force`,
`drm_debug_disable_explicit_sync`, `wayland_use_modifiers`/`vr_use_modifiers`,
`adaptive_sync`, `drm_single_plane_optimizations`. None fixed the button-bar
corruption (see #5). Session env to reach it:
`XDG_RUNTIME_DIR=/run/user/1001 WAYLAND_DISPLAY=gamescope-0 gamescopectl <convar> <val>`.

## 2026-07-04 (later): pivot to a sway seat + SSD library + monitor audio

Dropped the gamescope Big-Picture **kiosk** session in favor of a real WM on the
game seat — one you can debug from and launch games from normally. Also moved the
Steam library to SSD and got monitor audio working. **Current state: sway on the
5090 works, Steam + Proton run, monitor speakers work; the one open item is
per-game lag.**

### Architecture change: gamescope kiosk → sway

- seat-game runs a **sway** session (NixOS `programs.sway`; config via HM
  `wayland.windowManager.sway` with `package = null`) as agentydragon — non-GNOME,
  so no D-Bus clash with the seat0 SPICE GNOME session. Games get direct scan-out
  via **per-title gamescope** (`gamescope -f -- %command%`), not a kiosk session.
- NVIDIA/wlroots gotchas that cost time:
  - `--unsupported-gpu` is mandatory (sway refuses NVIDIA otherwise); set via
    `programs.sway.extraOptions`.
  - **Do NOT set `WLR_DRM_DEVICES` to a `/dev/dri/by-path/pci-…` node.** That env
    is a _colon-separated list_, so the PCI address's colons split it into garbage
    → "Found 0 GPUs, cannot create backend" → sway exits and _wedges the greeter_
    (it grabbed DRM, died, mutter couldn't reclaim). Unneeded anyway: the seat
    assignment already hands seat-game only card2 (the 5090).
  - `WLR_NO_HARDWARE_CURSORS=1`.
  - Same GDM 60 s silent-death trap as gamescope — a failed session Exec logs
    nothing. The **"Sway (debug)"** session (logs `sway -d` to
    `/tmp/sway-session.log`) is what cracked the WLR_DRM_DEVICES bug. Keep the
    debug-session-with-file-logging pattern for anything GDM launches.
- Driving the seat headlessly over SSH works well: `swaymsg -t get_tree` for
  windows, `grim -` for screenshots (pipe to a local file and view), `swaymsg
seat - cursor` for input. Env: `XDG_RUNTIME_DIR=/run/user/1001
WAYLAND_DISPLAY=wayland-1 SWAYSOCK=/run/user/1001/sway-ipc.1001.<pid>.sock`.

### Steam library on SSD (/games)

- Library was on `/mnt/tankshare` (tank-hdd virtiofs); Proton prefix creation
  (thousands of small files) crawled for minutes. Repurposed the decommissioned
  Longhorn disk (vdb, local-zfs SSD) into a **500 GB `/games`** library — grew
  100→500 GB imperatively via `qm` on atlas, reusing the `virtio1` slot so
  `/dev/vdb` doesn't rename.
- **Stellaris** (native Linux) dies on `libselinux.so.1` in the SLR sandbox →
  force Proton. First Proton launch then failed with `FileNotFoundError: …/
tracked_files` — a **half-migrated prefix** (moving the game copied `pfx/` but
  not `version`/`tracked_files`). Fix: `rm -rf compatdata/281990` and relaunch to
  rebuild the prefix fresh (fast on SSD).

### Monitor audio (DP passthrough)

- The seat had **no audio path**: only the SPICE virtual sink (routes to the SPICE
  console, muted). The display 5090's DP audio function wasn't passed through.
- Fix: pass the **whole** display-GPU device. Host `03:00.0` had only its GPU
  function passed; `03:00.1` (audio, same IOMMU group 16, already `vfio-pci`) was
  not. Changed `hostpci1` from `0000:03:00.0` → `0000:03:00` (qm on atlas + TF).
  Guest now sees `02:00.1` → ALSA "HDA NVidia" → PipeWire sink "GB202 … (HDMI)";
  `wpctl set-default <sink>` routes audio to the FV43U's built-in speakers.
  **Confirmed working.**
- Gotcha: after a _forced_ VM stop, `qm start` failed once with `/dev/vfio/14:
Device or resource busy` (compute-GPU IOMMU group not yet released) — just retry
  `qm start` after a few seconds.

### Per-game lag = cross-GPU copy (root-caused)

Stellaris felt like ~15 FPS on a 5090. Cause is **not** presentation overhead and
**not** GPU power — it's the **two-identical-5090** topology:

```text
stellaris.exe  → GPU0 (01:00.0)  6.6 GiB   ← the COMPUTE 5090 (DXVK rendered here)
sway/Xwayland/display → GPU1 (02:00.0)      ← the DISPLAY 5090 (monitor is on DP-4)
```

DXVK picks the first Vulkan device (GPU0, the compute card), but the monitor is
on GPU1, so **every frame is copied GPU0→GPU1 over PCIe** — that copy is the
bottleneck. Diagnose with `nvidia-smi --query-compute-apps=pid,process_name,gpu_bus_id,used_memory`
(the game's `gpu_bus_id` should match the display GPU) and `nvidia-smi pmon`.

Two identical GPUs give no clean per-app PCI selector (DXVK's `DXVK_FILTER_DEVICE_NAME`
can't tell them apart; NVIDIA Vulkan has none either). The standard fix is
**gamescope with `--prefer-vk-device`**, which pins render+display to one GPU:

- Launch option: `gamescope -f --prefer-vk-device 10de:2b85 -- %command%`
  (`--prefer-vk-device` mandatory — without it gamescope grabs the virtio GPU:
  `radv/amdgpu: failed to initialize device` / `vdrm_device_connect failed`).
- Needs a **raw** gamescope: the capSysNice-wrapped `/run/wrappers/bin/gamescope`
  dies in Steam's `no_new_privs` sandbox with `failed to inherit capabilities:
Operation not permitted`. Set `programs.gamescope.enable = true; capSysNice = false;`.
- The option must be set in the **Steam UI** — Steam Cloud reverts `localconfig.vdf`
  edits made over SSH.
- Also: sway defaulted DP-4 to **4K@60** though the FV43U exposes a **4K@144** mode
  (`swaymsg -t get_outputs`) — worth pinning 144 in the sway output config.

### Wind-down status (2026-07-04, end of session)

**Working + verified:** sway on the display 5090 (seat-game); `/games` 500 GB SSD
Steam library; monitor DP audio; Stellaris launches and runs under Proton.

**The one thing left to verify — per-game gamescope for the lag:** the raw
gamescope fix (`programs.gamescope.enable = true; capSysNice = false`) is
**built and live on wyrm2** — `command -v gamescope` is the unwrapped binary, the
`/run/wrappers/bin/gamescope` capSysNice wrapper is gone. What remains is purely
the Steam-side step: set the launch option **in the Steam UI** (not over SSH —
Steam Cloud reverts `localconfig.vdf` edits) to
`gamescope -f --prefer-vk-device 10de:2b85 -- %command%`, launch fresh, and
confirm `nvidia-smi --query-compute-apps` shows `stellaris.exe` on `02:00.0`
(display GPU) instead of `01:00.0` (compute) — that kills the cross-GPU copy.
Gotcha: `steam://rungameid` no-ops if the game is already running; stop it first.

**Branches (all unmerged at wind-down):**

- **#2891** `wyrm2-games-disk` — the `/games` SSD disk (nix + TF). Open PR.
- **`wyrm2-sway-seat`** — stacked on #2891; carries the sway session, the audio
  passthrough, grim, the raw-gamescope fix, and these notes. **No PR opened yet.**

**Still to do (cleanup, not blocking):** strip the now-unused gamescope kiosk bits
(`programs.steam.gamescopeSession`, the "Steam (debug)" + "Sway (debug)" sessions,
`gdm.debug`); pin DP-4 to 4K@144 in the sway config; harden the session-teardown
leak.

### 2026-07-05 — gamescope abandoned; display moved to 01:00.0 instead

Tried the gamescope launch option (`gamescope -f --prefer-vk-device 10de:2b85 --
%command%`) live — **it did not help.** Confirmed why: `--prefer-vk-device` only
takes `vendorID:deviceID`, and the two 5090s are identical (`10de:2b85`), so it
cannot disambiguate them — it just grabs whichever Vulkan device enumerates first
(the compute card, `01:00.0`), leaving the cross-PCIe copy in place. gamescope
3.16.24 has **no PCI-bus selector**, and NVIDIA's proprietary Vulkan honors no
per-app PCI device filter (Mesa's `MESA_VK_DEVICE_SELECT` layer isn't present /
doesn't apply to the NVIDIA ICD). So there is **no software way to pin the game to
a specific one of two identical GPUs** on this stack.

**The actual fix (simpler, no gamescope): move the monitor to the GPU the game
already renders on.** DXVK/Vulkan always picks the first-PCI device (`01:00.0`) and
we can't change that — so instead make `01:00.0` the _display_ GPU. Then
render==display==`01:00.0`, zero cross-PCIe copy, no compositor tricks.

Steps taken (commit on `wyrm2-sway-seat`):

- **Physical:** replug the FV43U DP cable from the `02:00.0` 5090 to the `01:00.0`
  5090 (passthrough mapping unchanged; only which card the cable is in).
- **Config swap** in `nix/nixos/hosts/wyrm2/default.nix`: `seat-game` udev pin
  `02:00.0`→`01:00.0`; `mutter-device-ignore` `01:00.0`→`02:00.0` (now hide the
  headless compute card, which is `02:00.0`); the "Steam/Sway (debug)"
  `WLR_DRM_DEVICES` node `02:00.0`→`01:00.0`.
- **Deploy:** `nixos-rebuild boot` + reboot (udev seat TAGs persist in the db, so a
  live `switch` won't fully re-home the seat — reboot required).

Post-reboot verification (all ✅): monitor connected on `card0-DP-1` (`01:00.0`);
`udevadm info` shows `ID_SEAT=seat-game` on `card0`/`01:00.0` and gone from
`02:00.0`; `loginctl seat-status seat-game` masters `card0`/`01:00.0`; greeter up
on seat-game. (Transient gotcha: right after boot, `loginctl seat-status` briefly
showed the _old_ card2 master — re-read a few seconds later for the settled view;
`udevadm info` is authoritative immediately.)

**Still to verify:** log into sway, launch Stellaris **plain (no gamescope launch
option — remove it)**, confirm `nvidia-smi --query-compute-apps=pid,process_name,gpu_bus_id`
shows `stellaris.exe` on `01:00.0` = the display GPU, with 5090-class FPS.

**Now-vestigial:** the raw-gamescope block + `programs.steam.gamescopeSession`
kiosk (and its `--prefer-vk-device` comment at ~L215, now describing the old
ordering) are dead for the lag fix — fold into the deferred gamescope cleanup.

### 2026-07-05 (later) — lag + audio confirmed, gamescope removed, UX applet, Stalker 2 freeze

**Lag fix confirmed in-game.** Stellaris launched **plain** (no gamescope) is
smooth with no artifacts on the display-on-`01:00.0` config. The GPU-role swap
(#2903) is the fix; gamescope is gone.

**Monitor DP audio working.** Passed the display card's audio function through:
`hostpci0` = whole `0000:01:00` device (not just `.0`), so the guest gets
`01:00.1`. PipeWire sink `alsa_output.pci-0000_01_00.1.hdmi-stereo`; the FV43U's
ELD is valid on ALSA `card1` (`NVidia_1`) PCM `eld#0.0`. **GOTCHA: NVIDIA HDMI
sinks default to 100.0 = 10000%** (PipeWire allows >100%) — deafening. Clamp with
`wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.3`; the waybar applet is capped at
`max-volume=100`.

**gamescope kiosk fully removed** (#2903). The "Steam (gamescope)"/"Steam (debug)"
greeter sessions opened the compute card (seat0-owned after the swap) → logind
`TakeDevice` denial → gamescope `Aborted (core dumped)` → **wedged greeter**.
Recover a wedged greeter with `systemctl restart display-manager`.

**Declarative seat UX (PR #2908):** `programs.foot` font size 11; **waybar**
replaces swaybar with a scroll/click **volume applet** (opens `pavucontrol`) +
tray; `pavucontrol` + XF86Audio keybinds. `programs.foot`/`programs.waybar`, not
hand-written `~/.config` files.

**Stalker 2 freeze (Proton hang, not storage).** Froze mid-game: display GPU at
**0% util** but holding 7.3 GB VRAM, `GameThread` spinning ~15% CPU, no GPU
Xid/hang in dmesg. Threads parked in `futex_wait_multiple`/`futex_wait_queue` =
**Wine fsync/futex deadlock** (a known Proton hang; Stalker 2 / UE5 is
freeze-prone). NOT a storage stall — the install is on `/mnt/tankshare`
(**virtiofs**, host-backed, responded instantly), not the `/games` SSD.
Relocating to `/games` helps load/stream perf but won't fix a Proton deadlock; if
freezes recur try Proton-GE or `PROTON_NO_FSYNC=1`. **Kill mechanics:** `pkill -f`
did **not** land (matching race); direct `kill -9 <pids>` on the Proton/wine tree
freed the VRAM (7.3 GB → 870 MiB).

**Seat teardown that works:** `loginctl terminate-session <id>` on the seat-game
user session cleanly kills sway + Steam + waybar and drops back to the greeter
(VRAM → ~250 MiB). Note `loginctl list-sessions` columns: SEAT is **field 4**
(field 3 is USER). The `systemd --user` manager session (no seat) lingers
benignly.

## 2026-07-11: GDM greeter freeze + two-greeter accumulation

**Symptom**: GDM seat-game greeter freezes on login; display stays frozen with each
re-attempt; display-manager restart required to recover.

**Device topology** (confirmed via udevadm, /sys/class/drm):

| Device | Driver     | PCI     | Seat      | Notes                                                     |
| ------ | ---------- | ------- | --------- | --------------------------------------------------------- |
| card0  | nvidia     | 01:00.0 | seat-game | Display GPU; DP-1 connected                               |
| card1  | virtio-pci | 00:01.0 | seat0     | SPICE virtual display                                     |
| card2  | nvidia     | 02:00.0 | seat0     | Compute GPU; `mutter-device-ignore`; no connected display |

(card1 lands numerically "between" the two NVIDIA cards because DRM enumeration
follows driver-load order, not PCI address order.)

**Phase 1 — original single-greeter freeze (21:29 UTC):**

- Only ONE greeter was running at this point (gnome-shell started by GDM 3305 at 20:45).
- User authenticated; GDM emitted `session-opened` at 21:29:25; no
  `ReadyForSessionToStart` came back within 60 s → "Session was cancelled" at 21:30:26.
- sway-session.log has **zero seat-game entries** — sway never launched. The block is
  entirely at the GDM greeter level (gnome-shell never calling `ReadyForSessionToStart`).
- `enable-animations=false` dconf is correctly applied (verified earlier) but apparently
  insufficient to make gnome-shell skip the render-loop dependency for the
  ReadyForSessionToStart callback. Frame clock stall (NVIDIA vblank not delivered?) or
  some other render-side block is the residual unknown.
- Sunshine (PID 233050) was later found running as `gdm-greeter-3` inside the greeter
  session — its role in the freeze is unconfirmed (was not present during the 21:29
  attempt per the log timeline, but appeared afterward).

**Phase 2 — two-greeter accumulation (from 22:15 onward):**

After the 21:30 session cancellation, GDM killed the PAM session worker (PID 97927) but
**left gnome-shell 3455 running**. It sat in a zombie/frozen state for 45 minutes.

At 22:15:36 gnome-shell 3455 finally disconnected from GDM. The seat-game display (c2)
had two internal session objects associated with it — one for the greeter program
(gnome-shell 3455) and one for the gdm-launch-environment wrapper (PID 3321). Both
dying in the same event loop tick caused two independent `GdmDisplay: initiating
display self-destruct` sequences, and `GdmLocalDisplayFactory` created a new seat-game
display for each one:

1. gnome-shell 3455 disconnects → `GdmDisplay: Greeter exited` → self-destruct #1 →
   factory adds DISPLAY_NEW_1 (status: PREPARING)
2. Self-destruct #1 sends SIGTERM to gdm-launch-environment worker PID 3321
3. PID 3321 dies → `GdmLaunchEnvironment: conversation stopped` → `GdmDisplay: Greeter
stopped` → self-destruct #2 → factory adds DISPLAY_NEW_2

`GdmLocalDisplayFactory` has no dedup: it creates a new display for each "seat failed"
signal without checking whether one is already being prepared for the same seat. Both
new displays land on seat-game → two simultaneous greeter gnome-shell processes both
opening card0 for KMS.

```
# loginctl during the two-greeter state
c5 60578 gdm-greeter   seat-game 240655 greeter
c6 60580 gdm-greeter-2 seat-game 240663 greeter
# Both gnome-shell PIDs have card0 open with identical flags (02104002)
```

Only one can hold the DRM master token; the other can't render. The display appears
frozen because whichever gnome-shell lost the DRM master token can't display anything.

The root of the cascade is the original 21:29 freeze: gnome-shell 3455 was never killed
after the session cancellation, so when it eventually died it triggered both cleanup paths
simultaneously. If GDM had killed the greeter at 21:30 along with the session worker, the
two paths would have died together under a controlled `finish display` and no new displays
would have been created (same as how the seat0 background display was cleanly destroyed at
20:46 without spawning a replacement).

**Recovery**: `systemctl restart display-manager` (kills seat0 sway session; user
must reconnect SPICE). Alternatively kill one duplicate first:
`loginctl terminate-session c6` — if GDM doesn't respawn it, the remaining greeter
is clean.

### 2026-07-11 23:09 — non-destructive recovery: park the seat (WORKED)

By 22:39 the two-greeter pile had self-healed to a single greeter: one gnome-shell
exited, both self-destruct paths fired, but `create_display`'s dedup
("Ensure we don't create the same display more than once",
`gdm-local-display-factory.c:960` in 49.2) caught the second request and reused the
surviving session (journal: "session c5 found, activating").

To drop the remaining (frozen) greeter **without touching seat0**, we made seat-game
non-graphical so GDM has nothing to respawn onto. Source facts (GDM 49.2 +
systemd 258, clones in `/code/github.com/{GNOME/gdm,systemd/systemd}`):

- Both greeter-restart paths (`GDM_DISPLAY_FAILED` and `GDM_DISPLAY_FINISHED` in
  `on_display_status_changed`) go through `ensure_display_for_seat()`, which for
  non-seat0 seats returns early when `sd_seat_can_graphical() == 0`. With
  CanGraphical=no, respawn is impossible regardless of how the greeter dies.
- `on_seat_properties_changed` (CanGraphical → false) and `on_seat_removed` both call
  `delete_display()` → display store unref → `gdm_display_dispose` → SIGTERM to the
  launch-environment process group. So flipping CanGraphical also _initiates_ a clean
  teardown.
- **Gotcha**: logind never clears a live Device's master flag —
  `manager_add_device()` (`logind-core.c`): `/* we support adding master-flags, but
not removing them */`. Removing only `TAG-="master-of-seat"` changes udev state but
  logind keeps `CanGraphical=yes` forever. The flag only clears when the Device is
  **freed**, which `manager_process_seat_device()` does when the device loses the
  `seat` current-tag (sticky `TAGS=` keep the uevent routable to logind's tag-filtered
  monitor — that's the designed purpose of sticky tags).

Procedure (as root on wyrm2):

```bash
cat > /run/udev/rules.d/98-park-seat-game.rules <<'EOF'
SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:01:00.0", TAG-="seat", TAG-="master-of-seat"
EOF
udevadm control --reload
udevadm trigger --action=change --subsystem-match=drm \
  --parent-match=/sys/devices/pci0000:00/0000:00:1c.0/0000:01:00.0
```

Result: logind freed the card0 Device, seat-game disappeared, GDM logged
`delete_display` → SIGTERM to greeter pgroup → session worker exited status 0 →
greeter dyn-user deallocated — and **no** "display for seat seat-game requested"
afterward. seat0 sway session untouched; no process holds `/dev/dri/card0`.

**Unpark** (spawns one fresh greeter — do this when ready to debug the freeze):

```bash
rm /run/udev/rules.d/98-park-seat-game.rules
udevadm control --reload
udevadm trigger --action=change --subsystem-match=drm \
  --parent-match=/sys/devices/pci0000:00/0000:00:1c.0/0000:01:00.0
```

The rule lives in `/run`, so a reboot also unparks. `ID_SEAT` stays `seat-game`
(from `/etc/udev/rules.d/72-seat-game.rules`), so card0 never migrates to seat0
where sway could hotplug-grab it.

**Next diagnostic steps (not yet done):**

- With a clean single greeter, attach a D-Bus monitor for the `ReadyForSessionToStart`
  signal: `dbus-monitor --system "type=method_call,interface=org.gnome.DisplayManager.Session"`.
- Also send `kill -USR1 <gnome-shell-pid>` immediately after `session-opened` to
  capture a JS backtrace of where gnome-shell is blocked.
- Check if disabling Sunshine in the greeter config changes the freeze behavior.

### 2026-07-11 23:24 — freeze caught live: it's the conflicting-session dialog

Fresh greeter (c7, gnome-shell 486153) after unparking. Login attempt at 23:24
(GNOME session type): auth succeeded, `session-opened` emitted 23:24:17, then no
`GdmManager: Will start session when ready` — the known freeze, caught with
instrumentation attached this time.

Live evidence:

- `eu-stack -p 486153`: main thread idle in `ppoll` inside `g_main_loop_run` —
  **not** wedged in any NVIDIA/DRM ioctl. All threads idle.
- In-process JS dump (`gdb -p 486153 -batch -ex 'call (void) gjs_dumpstack()'` —
  non-fatal, unlike the SIGTRAP/SIGABRT crash dumper; note gnome-shell has **no**
  SIGUSR1 stack handler, the earlier SIGUSR1 plan was wrong): single frame at
  `init.js:21` (main-loop hook). JS ran to completion and is waiting for an event.
- No JS errors in the greeter journal.

Root cause (gnome-shell 49.4 `js/gdm/loginDialog.js`): `_onSessionOpened` calls
`_findConflictingSession(sessionId)`, which scans logind for any wayland/x11
session in state active/online **owned by the same user**. If found, it opens
`ConflictingSessionDialog` ("user is already logged in — force stop?") and returns
**without** calling `StartSessionWhenReady`, waiting for dialog input.
`_CONFLICTING_SESSION_DIALOG_TIMEOUT = 60` — after 60 s the dialog closes and the
auth prompt resets; GDM's own 60 s `session-opened` timer fires around the same
time → "Session was cancelled".

The seat0 sway/SPICE session (user agentydragon, type wayland, active since 20:46)
has been running during **every** freeze — so every seat-game login raised this
dialog. Whether the dialog actually painted on the FV43U is the remaining question:
if it did not (stale login-prompt frame stays on screen), there is _also_ a
repaint/frame-clock issue; if it did, the "freeze" was just an unanswered dialog.

Consequence: to log into seat-game as the same user, either answer the dialog
(force-stop kills the seat0 session) or log out of the seat0 sway session first —
then no conflict exists and the login should proceed directly.

### 2026-07-11 — greetd ruled out for seat-game

Considered swapping seat-game's greeter to greetd (no conflict check). Rejected after
reading greetd source (`/code/github.com/kennylevinsen/greetd`):

- **Hardcoded seat**: `greetd/src/session/worker.rs:216` puts `XDG_SEAT=seat0` in the
  PAM env for every session it starts — no config override. greetd can only ever
  create seat0 sessions; it cannot target seat-game.
- **VT-based**: `greetd/src/terminal/mod.rs` drives sessions via `KDGRAPHICS`/`KDTEXT`
  ioctls on `/dev/ttyN`. seat-game has `CAN_TTY=0` (no VTs), so there is no terminal
  for greetd to manage there.

greetd is single-seat/seat0/VT-oriented. (Note: a later source review found SDDM and
LightDM _do_ support per-logind-seat multi-seat and have no same-user veto — see
<greeter_multiseat_research.md>. Only greetd is disqualified on the VT/seat0 axis.)
Options that remain, to run two simultaneous same-machine graphical sessions:

1. **Separate user for the gaming seat** (e.g. `games`): zero patching. GDM's multi-seat
   greeter is _designed_ for two different users at once — no conflict dialog fires
   because `_findConflictingSession` matches only same-user sessions. Cost: a second
   home/config.
2. **Patch the gnome-shell greeter**: make `_findConflictingSession`
   (`js/gdm/loginDialog.js`) skip sessions on a different `Seat` than the greeter's.
   Keeps same-user-both-seats. Cost: a gnome-shell rebuild (the JS lives in a gresource
   bundle, so it's a source patch + repackage, not a drop-in file).
3. **Greeterless autologin on seat-game**: a custom launcher (PAM + `pam_systemd` with
   `XDG_SEAT=seat-game`) that execs sway directly, with GDM no longer managing that seat.
   GDM has no per-seat exclude, so this means taking seat-game out of GDM's view — most
   bespoke of the three.

### 2026-07-12 — decision: swap GDM → SDDM (staged, switch pending)

Chose SDDM over the alternatives. Rationale from <greeter_multiseat_research.md>: SDDM
does per-logind-seat multi-seat, sets `XDG_SEAT` per seat, gates VT usage on seat0 (so
the VT-less seat-game is fine), supports a Wayland greeter, and — unlike GDM — has **no
same-user conflict check**. So the same user can hold a live session on seat0 (SPICE)
and seat-game (physical) at once with no dialog. The seat-game session is sway (no fixed
D-Bus names), so it never collides with the seat0 GNOME session on the shared per-user
bus.

Staged in `nix/nixos/hosts/wyrm2/default.nix`:

- `services.displayManager.gdm.enable = lib.mkForce false` (gdm.enable is set in the
  shared `gui.nix`; force-disabled here so other hosts keep GDM).
- `services.displayManager.sddm = { enable = true; wayland.enable = true; }`.
- Removed the GDM-greeter dconf tweaks (idle-delay=0, enable-animations=false).

Validated at eval only (no switch yet): resolved `gdm.enable=false`,
`sddm.enable=true`, `sddm.wayland.enable=true`, and the `display-manager` unit's start
script references `sddm`. Two build hiccups were **unrelated infra**, not this change:
`nixos-rebuild` didn't pass the `fetch-closure` experimental feature; then the root-only
attic netrc (`/run/secrets/rendered/attic-netrc`) gave a 401 to a non-root eval. The
real `sudo nixos-rebuild switch` runs as root and reads that netrc, so gaffer/attic
fetch is fine there.

**Switch is deferred — the user will run it.** `sudo nixos-rebuild switch --flake
~/code/ducktape#wyrm2`. It restarts display-manager → the seat0 SPICE desktop dies and
SDDM comes up. A failed build aborts before activation (safe); root SSH survives
regardless. Revert = uncomment the GDM lines + rebuild.

**Open risks to check after the switch:**

- **SDDM Wayland greeter on the NVIDIA seat-game** — unverified. The greeter compositor
  (`Wayland.CompositorCommand`, default weston) must acquire DRM master as a
  `greeter`-class session on card0. If it won't come up, fall back to per-seat autologin
  (skip the greeter and launch sway directly) or SDDM's X11 greeter.
- **No-blank for the FV43U KVM** — the GDM `idle-delay=0` guarantee is gone. If the KVM
  reverts to USB-C because the seat-game greeter blanks, add an equivalent no-DPMS to the
  SDDM greeter compositor / sway `swayidle`.

## Open questions

- **Did the ConflictingSessionDialog ever render on the physical display?** If not,
  there is a separate repaint stall in the greeter (frame-clock / NVIDIA), on top of
  the dialog logic. Distinguish by logging out of seat0 sway and retrying login —
  a fade-then-session-start means rendering is fine.
- **Sunshine in greeter session**: Sunshine runs as `gdm-greeter-*` user when the seat
  is unoccupied. Investigate whether it should be suppressed during greeter mode.
- **Harden the session-teardown leak** so re-logins stop colliding: logind
  `KillUserProcesses` scoped to the seat-game session, or a session-exit hook that
  reaps the compositor/Steam. Currently manual (also bit the sway seat).
- Is the spare USB A→B cable actually good? (First-port "cable is bad"
  errors vs. port flakiness — untested since the mux never engaged.)
- Does the FV43U KVM binding survive monitor power cycles?

## 2026-07-16: SDDM live diagnosis — seatphysical greeter dead + seat0 GNOME lock dead

Picked this up on wyrm2 with SDDM live (up since 2026-07-14 02:10). Two problems,
both traced to root cause from source. **Renamed the seat `seat-game` → `seatphysical`
this session** (see below); entries above this line predate the rename and refer to
the old `seat-game` name.

### Symptom recap

- **seatphysical (physical NVIDIA monitor): black, no session.** `loginctl list-seats`
  shows `seatphysical` with `CanGraphical=yes` but `Sessions=` empty. The SDDM greeter
  never comes up there. seat0 (SPICE GNOME) logs in fine.
- **seat0 GNOME screen lock: unavailable.** Apps (`gnome-session`, `gsd-usb-protection`)
  report `org.gnome.Shell.ScreenShield was not provided by any .service files`;
  `loginctl lock-session 17` returns 0 but `LockedHint=no` and nothing locks. Started
  with the GDM→SDDM swap.

### Root cause 1 — seatphysical greeter: systemd-258 varlink `CreateSession` rejects it

`sddm-unwrapped-0.21.0` + `systemd 258` (logind `CreateSession` is now varlink).
Current-boot journal, seatphysical greeter attempt:

```text
QDBusObjectPath: invalid path "/org/freedesktop/DisplayManager/Seat-game"
QDBusConnection: Could not emit signal ...SeatAdded: Marshalling failed
pam_systemd(sddm-greeter:session): CreateSession() varlink call: org.varlink.service.InvalidParameter
sddm-helper-start-wayland: weston: fatal: environment variable XDG_RUNTIME_DIR is not set
```

Chain: `CreateSession` is rejected → no logind session → no `XDG_RUNTIME_DIR` → the
greeter's `weston` aborts → no greeter renders → monitor stays black.

Two distinct defects:

1. **D-Bus hyphen (secondary).** `DisplayManager::seatPath` (SDDM
   `src/daemon/DisplayManager.cpp:45`) builds `"/org/freedesktop/DisplayManager/Seat"
   - seatName.mid(4)`, so `seat-game`→`Seat-game`, an invalid object-path element
(hyphen). This only breaks the `SeatAdded`**signal**, not session creation — but
it is trivially avoidable.`seatphysical`→`Seatgame`(valid; also passes logind's`seat_name_is_valid`, pure alnum). **Fixed by the rename.**
2. **varlink `CreateSession` `InvalidParameter` (primary blocker).** systemd 258
   `src/login/logind-varlink.c:187-229` validates `Seat`/`TTY`/`VTNr` and returns
   `InvalidParameter` naming the offending field. SDDM's Wayland greeter on a
   non-seat0 seat never sets `PAM_TTY` (it only sets it for X11 or when `XDG_VTNR`
   is present, and `XDG_VTNR` is gated to seat0 in `Greeter.cpp:214`). By static
   reading the params _should_ pass (seat exists, no VT, vtnr 0) — so the exact
   rejected field is not yet proven. **Added `SYSTEMD_LOG_LEVEL=debug` on
   `systemd-logind`** so the next boot logs which field logind refuses. This is the
   make-or-break datum: if it is a fixable PAM/tty detail or an SDDM version bug we
   fix in place. There is **no clean fallback** — GDM is out (see the constraints +
   DM landscape in "Next steps" below); if SDDM 0.21's non-seat0 greeter path is
   fundamentally incompatible with systemd-258 varlink logind, the only options left
   are LightDM or a bespoke `cage` + `gtkgreet` per-seat greeter.

### Root cause 2 — seat0 GNOME lock: `ScreenShield` never claims its D-Bus name

gnome-shell 49.4 is the live compositor (owns `wayland-0`) but does **not** own
`org.gnome.Shell.ScreenShield`, so every lock path fails. Mechanism (source:
`/code/github.com/GNOME/gnome-shell` tag 49.4):

- `screenShield.js:145 _getLoginSession()` awaits
  `loginManager.getCurrentSessionProxy()` then `connectSignal('Lock', …)`. If the
  proxy is null it throws and the shield never wires Lock/Unlock.
- `loginManager.js:127` reads `XDG_SESSION_ID`; gnome-shell's env has **none**
  (it runs as the `org.gnome.Shell@wayland.service` systemd --user unit, in
  `user@1001.service` — the seatless manager scope, not `session-17.scope`). It
  falls back to `/org/freedesktop/login1/session/auto`.
- Live checks: `loginctl show-user agentydragon` has `Display=17`, and `session/auto`
  resolves to `17` from a normal caller — so resolution _looks_ fine, yet
  `ScreenShield` is still unowned. Why the shield init fails despite that is **not
  yet proven** and needs a live attach to gnome-shell (dump whether
  `getCurrentSessionProxy()` returned null and any JS rejection). Under GDM the lock
  worked, so this is SDDM-session-registration-specific (missing `XDG_SESSION_ID`
  into the systemd --user env is the leading suspect).

### Done this session (config, no reboot yet)

- Renamed `seat-game` → `seatphysical` in live config + current-state docs
  (`nix/nixos/hosts/wyrm2/default.nix` udev rules `72-seatphysical.rules` +
  `ENV{ID_SEAT}=seatphysical`, `nix/home/hosts/wyrm2.nix`, `cluster/terraform/main/
proxmox-vms.tf`, `desk/`). Historical log entries above keep the old name.
- Added `systemd.services.systemd-logind.environment.SYSTEMD_LOG_LEVEL = "debug"`
  (marked `DEBUG(added 2026-07-16)`, remove once the greeter is solved).

### Constraints (user decisions, 2026-07-16)

Three hard "no"s — together they leave SDDM as essentially the only mainstream
path, with no clean fallback:

- **No autologin on seatphysical** — it must be a real login gate.
- **No patching GDM / gnome-shell** — the GDM `_findConflictingSession` patch
  (2026-07-11 entries) is off the table.
- **No separate user for seatphysical** — same user (agentydragon) on both seats.

Goal: two working seats (seat0 GNOME on SPICE + seatphysical on the NVIDIA 5090)
**and** a working screen lock on each.

### DM landscape under these constraints

**GDM is fully out.** Its only blocker was the same-user conflict dialog, and its
two escapes — patch gnome-shell, or a separate user — are both vetoed. So GDM
cannot do same-user-two-seats here at all.

That leaves **SDDM as the committed path** — the only mainstream DM with
per-logind-seat greeters and no same-user veto. Committing to it means landing
**two** independent SDDM/GNOME-integration fixes, neither yet proven here:

1. **Greeter on seatphysical** — the varlink `CreateSession` rejection (the reboot
   captures the exact field).
2. **seat0 GNOME lock** — `ScreenShield` never claims its D-Bus name (worked under
   GDM, broke under SDDM; leading suspect `XDG_SESSION_ID`/session-scope
   propagation). Note seatphysical's _own_ lock is swaylock (wlroots,
   DM-independent), so it is not at risk — only the seat0 GNOME lock is.

No clean drop-in fallback remains. If SDDM hits a wall on either fix, the only
options left are worse, and both are fresh investigations:

- **LightDM** — same per-seat architecture, no same-user veto, but a weak Wayland
  greeter (its greeters are X11); a real (non-autologin) greeter on this NVIDIA
  seat is untested.
- **Bespoke per-seat greeter** — a custom unit running `cage` (wlroots kiosk) +
  `gtkgreet` with a PAM login on seatphysical. Satisfies every constraint but is
  the most hand-rolled (custom unit + PAM stack) — essentially a mini-DM for the
  seat.

### Reboot procedure

**Safety net (verified 2026-07-16):** `sshd` is active, key-only
(`PermitRootLogin prohibit-password`, `PasswordAuthentication no`), independent of
the graphical session. Reach wyrm2 from atlas/laptop at `192.168.1.72` (LAN) or
`10.42.0.20` (nebula) — survives a display-manager restart. Recovery if the greeter
crash-loops: `sudo systemctl restart display-manager`, the park-the-seat udev trick
(2026-07-11 entry), or roll back (`sudo nixos-rebuild switch --rollback` / previous
boot-menu generation).

1. `sudo nixos-rebuild boot --flake ~/code/ducktape#wyrm2`, then `reboot`. Use
   `boot` not `switch` — a reboot is required anyway to re-home the udev seat TAG
   `seat-game` → `seatphysical` (TAGs persist in the udev db), and `boot` avoids a
   mid-session DM restart that would drop seat0 twice.
2. Capture the diagnostic over SSH (not dependent on the graphical session):

   ```bash
   loginctl list-seats                    # expect: seatphysical (no hyphen)
   loginctl seat-status seatphysical      # is the NVIDIA card mastered here?
   journalctl -b -u display-manager | grep -iE 'seatphysical|CreateSession|weston|Seat'
   journalctl -b -u systemd-logind | grep -iE 'CreateSession|InvalidParameter|Seat|VTNr|TTY'
   ```

   The **rejected field** (`Seat`/`TTY`/`VTNr`) from the last command is the decision
   point (see the fallback ladder). Also note whether the hyphen fix alone changed
   greeter behavior.

3. **seat0 lock** (independent of the reboot — broken live already): live-attach
   gnome-shell to prove why `ScreenShield` won't own its D-Bus name. Leading suspect:
   `XDG_SESSION_ID` not reaching the `org.gnome.Shell@wayland.service` systemd
   `--user` unit (it runs in the seatless `user@1001.service` scope). Fix likely
   propagates the login session id/env into the user manager.

Remove the `SYSTEMD_LOG_LEVEL=debug` logind drop-in once the greeter is solved.
