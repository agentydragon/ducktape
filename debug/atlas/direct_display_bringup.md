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

## Open questions

- **Per-game lag** (root-caused, fix in progress): cross-GPU copy — see above.
  Fix = raw gamescope (`capSysNice=false`) + `--prefer-vk-device 10de:2b85` launch
  option to pin the game to the display GPU. Also bump DP-4 to 4K@144 in sway.
- **Harden the session-teardown leak** so re-logins stop colliding: logind
  `KillUserProcesses` scoped to the seat-game session, or a session-exit hook that
  reaps the compositor/Steam. Currently manual (also bit the sway seat).
- Is the spare USB A→B cable actually good? (First-port "cable is bad"
  errors vs. port flakiness — untested since the mux never engaged.)
- Does the FV43U KVM binding survive monitor power cycles?
- Does mutter handle nvidia-primary + virtio-secondary cleanly (SPICE
  desktop must stay usable — hard requirement)?
