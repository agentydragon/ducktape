# Rugged Auto-Rotate Investigation

Date: 2026-04-25
Host: `rugged`
Hardware: Dell Pro Rugged 12 Tablet RA02260

## Symptom

GNOME auto-rotate is not rotating the built-in display, even though this machine is a tablet and auto-rotate reportedly worked at some point in the past.

## Findings So Far

### Host configuration is correct

- The `rugged` NixOS host configuration enables IIO sensor proxy:
  - `nix/nixos/hosts/rugged/default.nix`
  - `hardware.sensor.iio.enable = true;`
- GNOME rotation lock is currently off:
  - `org.gnome.settings-daemon.peripherals.touchscreen orientation-lock false`

### Kernel/IIO sensor path works

- `/sys/bus/iio/devices` contains the expected HID sensor devices:
  - `als`
  - `magn_3d`
  - `accel_3d`
  - `gyro_3d`
  - `incli_3d`
  - `dev_rotation`
  - `relative_orientation`
- `accel_3d` produces live raw values.
- `iio-sensor-proxy.service` is active and running.
- `busctl` reports:
  - `HasAccelerometer = true`
  - `HasAmbientLight = true`

### Tablet-mode switch works

- The kernel input device `Intel HID switches` is present.
- Current `/proc/bus/input/devices` block:

```text
I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="Intel HID switches"
S: Sysfs=/devices/platform/INTC107B:00/input/input13
H: Handlers=event9
B: EV=21
B: SW=2
```

- `SW=2` means the device exposes only `SW_TABLET_MODE`.
- Live switch-state read from `/dev/input/event9` returned:

```text
/dev/input/event9 0x2 [1]
```

- Interpretation:
  - `SW_TABLET_MODE` is active right now.
  - GNOME/Mutter is being told the machine is in tablet mode.

### Sensor orientation events work when explicitly claimed

- `monitor-sensor` initially appeared with orientation unset:

```text
=== Has accelerometer (orientation: undefined, tilt: undefined)
```

- After moving the device, `monitor-sensor` reported valid orientation transitions, including:
  - `Accelerometer orientation changed: normal`
  - `Accelerometer orientation changed: left-up`
  - `Accelerometer orientation changed: right-up`

- This proves:
  - The accelerometer is not missing.
  - The sensor proxy can derive orientation correctly.
  - Physical rotation is reaching userspace correctly.

## Suspicious Runtime Signals

- In the user journal, GNOME logged:

```text
gsd-power: Release of light sensors failed: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Not Authorized: Sensor claim not allowed
```

- A direct manual D-Bus claim against `net.hadess.SensorProxy` succeeded, so the sensor service is not globally broken.
- This suggests a GNOME-side integration or policy/regression issue rather than missing hardware support.
- The `gsd-power` light-sensor warning is likely not the root cause by itself; similar
  warnings appear on otherwise-working GNOME systems and do not explain the missing
  builtin-panel rotation.

## Mutter / GNOME Boundary

- `org.gnome.Mutter.DisplayConfig.GetCurrentState` shows the built-in display as `eDP-1`.
- Direct D-Bus query now shows:
  - `org.gnome.Mutter.DisplayConfig.PanelOrientationManaged = true`
  - `HasExternalMonitor = true`
- Live `GetCurrentState` continues to show the built-in logical monitor transform as `0` (normal), even after physical rotation events.
- Mutter source references indicate orientation changes are applied through the monitor manager on `orientation-changed`.

### Relevant Mutter source path

From `/tmp/mutter-src` (commit `e14c662`):

- `src/backends/meta-monitor-manager.c`
  - `update_panel_orientation_managed()` sets panel orientation managed only when all are true:
    - `clutter_seat_get_touch_mode(seat)`
    - `meta_orientation_manager_has_accelerometer(...)`
    - built-in monitor exists
  - `orientation_changed()` returns early if `panel_orientation_managed` is false.
  - `handle_orientation_change()` computes the requested transform from accelerometer orientation and applies a temporary monitor config.
- `src/backends/meta-orientation-manager.c`
  - Mutter only receives live `AccelerometerOrientation` property updates from `iio-sensor-proxy` when it has successfully claimed the accelerometer.
- `src/tests/monitor-orientation-tests.c`
  - Mutter’s own tests explicitly expect the built-in panel to continue rotating correctly even with an external monitor connected, as long as the lid is open.

### Additional live checks

- DRM/KMS connector `eDP-1` reports:

```text
panel orientation:
  enums: Normal=0 Upside Down=1 Left Side Up=2 Right Side Up=3
  value: 0
```

- This rules out a hidden panel-orientation correction causing the physical rotation to appear as a no-op.

- While watching `org.gnome.Mutter.DisplayConfig` during tablet rotation:
  - no `MonitorsChanged` signal was observed
  - the built-in display transform remained unchanged in `GetCurrentState`

### Manual Mutter rotate works

- A direct D-Bus `ApplyMonitorsConfig` verify call succeeded for a temporary `90°`
  transform on `eDP-1`.
- A direct D-Bus `ApplyMonitorsConfig` temporary apply also succeeded.
- `GetCurrentState` immediately reflected the built-in logical monitor transform
  changing from `0` to `1` (`90°`), and a second temporary apply cleanly restored
  it to `0`.

This rules out:

1. Broken monitor-config generation for rotated builtin layouts.
2. Broken monitor-config apply for the current two-monitor session.
3. External-monitor presence as a hard blocker for built-in panel rotation.

This narrows the remaining fault to:

1. Mutter is not actually receiving / processing the live `orientation-changed`
   path despite sensor orientation changes existing in `iio-sensor-proxy`.
2. Mutter is failing to claim or stay subscribed to the accelerometer, so it never
   receives the live `AccelerometerOrientation` property updates needed to trigger
   rotation.

### Negative tests after narrowing

- Disconnecting the external `DP-1` monitor did **not** restore auto-rotate.
- Restarting `iio-sensor-proxy.service` did **not** restore auto-rotate.
- Temporarily disabling the `display-scale-switcher` GNOME Shell extension did
  **not** restore auto-rotate.
- Temporarily disabling **all** user GNOME Shell extensions did **not** restore
  auto-rotate.
- Monitoring the system D-Bus traffic for `net.hadess.SensorProxy` while:
  - toggling `orientation-lock` `true` -> `false`
  - and physically rotating the tablet
    showed **no** `ClaimAccelerometer` or `ReleaseAccelerometer` calls from the
    live session.
- A `strace` attached to the running `gnome-shell` process during the same test
  also showed:
  - `orientation-lock` dconf change notifications arriving on the session bus
  - ordinary session D-Bus traffic continuing normally
  - **no** traffic containing `net.hadess.SensorProxy`,
    `ClaimAccelerometer`, `ReleaseAccelerometer`, or
    `AccelerometerOrientation`

These additional tests rule out:

1. External-monitor presence as the practical blocker in the current setup.
2. A stale `iio-sensor-proxy` process or a one-shot missed service startup.
3. User-installed GNOME Shell extensions as the immediate cause.
4. Live auto-rotate being merely "slow" or delayed behind the observed rotation.
5. The possibility that Mutter is attempting sensor D-Bus calls that were simply
   missed by higher-level monitoring.

### Session / startup observations

- The active graphical session is `loginctl` session `2` on `seat0`, `Type=wayland`,
  `Active=yes`, `Service=gdm-password`.
- GNOME Shell registered a polkit agent under one session identifier, then later
  re-registered under `unix-session:2` after a compositor restart during login.
- GNOME logs also included:

```text
Missing required core component Settings, expect trouble…
```

- `gnome-settings-daemon` components are running in the final session, so this
  message does not mean the settings stack is absent, but it does suggest a noisy
  or unstable startup sequence.

Current working theory:

1. The hardware path is healthy.
2. The tablet-mode switch is healthy.
3. Sensor orientation derivation is healthy.
4. Manual display rotation inside Mutter works, so monitor-config generation/apply
   is healthy.
5. GNOME/Mutter is failing somewhere in the live sensor subscription or
   `orientation-changed` path inside the compositor/session build.

### Stronger Mutter-specific hypothesis

The absence of any `ClaimAccelerometer` / `ReleaseAccelerometer` traffic during the
live test suggests Mutter is not actively claiming the sensor anymore in the current
session.

Reading the source makes a startup race plausible:

- `MetaOrientationManager` only receives live orientation property updates after a
  successful `ClaimAccelerometer`.
- `MetaMonitorManager::orientation_changed()` unconditionally calls
  `meta_orientation_manager_inhibit_tracking()` on the first signal
  (`initial_orient_change_done` path).
- If startup ordering leaves the monitor manager in a state where that first signal
  lands after panel orientation has already become managed, the inhibit count may
  remain elevated in a way that suppresses future sensor claims for the rest of the
  session.

This matches the observed behavior well:

1. Manual monitor rotation still works.
2. Sensor-derived orientation exists when another client claims the sensor.
3. The live GNOME session shows no sensor claim/release activity of its own.
4. Service restart and settings toggles do not recover the session.

### Core-dump proof from the live broken session

A `gcore` dump of the running `gnome-shell` / Mutter process was inspected with
the matching `gnome-shell` and `mutter` debug outputs.

Recovered live Mutter state from the dump:

- `MetaContextPrivate.backend` points to a valid backend object.
- `MetaBackendPrivate.orientation_manager` points to a valid
  `MetaOrientationManager`.
- `MetaBackendPrivate.monitor_manager` points to a valid
  `MetaMonitorManager`.

The critical `MetaOrientationManager` fields in the broken session were:

- `has_accel = 1`
- `orientation_locked = 0`
- `should_claim = 0`
- `is_claimed = 0`
- `orientation = META_ORIENTATION_UNDEFINED`
- `inhibited_count = -1`

The paired `MetaMonitorManagerPrivate` fields were:

- `initial_orient_change_done = 0`
- `power_save_mode = META_POWER_SAVE_ON`
- `power_save_inhibit_orientation_tracking = 0`

Interpretation:

1. Mutter sees the accelerometer (`has_accel = 1`).
2. Rotation lock is not blocking it (`orientation_locked = 0`).
3. Power-save inhibition is not blocking it.
4. Mutter has never processed its first orientation event in this session
   (`initial_orient_change_done = 0`).
5. Yet the orientation manager's internal inhibit counter is already negative
   (`inhibited_count = -1`).

That is a concrete broken internal state. It directly explains why Mutter never
claims the accelerometer in this session:

- `sync_accelerometer_claimed()` only sets `should_claim = true` when
  `self->inhibited_count == 0`.
- A value of `-1` therefore suppresses `ClaimAccelerometer` permanently.

Relevant source lines:

- `src/backends/meta-orientation-manager.c`
  - `sync_accelerometer_claimed()` computes
    `should_claim = self->iio_proxy && self->inhibited_count == 0`
    (lines `287`-`326` in the inspected source tree).
  - `meta_orientation_manager_uninhibit_tracking()` blindly decrements
    `self->inhibited_count--` with no lower-bound guard
    (lines `527`-`532`).
- `src/backends/meta-monitor-manager.c`
  - `update_panel_orientation_managed()` calls
    `meta_orientation_manager_uninhibit_tracking()` immediately when panel
    orientation becomes managed
    (lines `1096`-`1131`).
  - In the captured broken session, `panel_orientation_managed = true` while
    `initial_orient_change_done = 0`, which means the negative counter was
    reached before Mutter ever handled its first orientation event.

This is substantially stronger than the earlier "maybe a startup race" theory:
the live broken session contains the exact wedged state preventing sensor claims.

### Refined startup-race interpretation

The core data and source together point to a specific ordering bug rather than a
generic random wedge.

Relevant ordering:

1. `MetaOrientationManager` starts with `inhibited_count = 0`.
2. If `iio_proxy_ready()` runs while the count is still `0`, it calls
   `sync_accelerometer_claimed()` and Mutter can claim the accelerometer.
3. The monitor manager treats the first `sensor-active` / `orientation-changed`
   signal specially and calls `meta_orientation_manager_inhibit_tracking()`,
   incrementing the counter.
4. Separately, when `update_panel_orientation_managed()` decides that tablet
   auto-rotation should be active, it calls
   `meta_orientation_manager_uninhibit_tracking()`, decrementing the counter.

This only works if those steps happen in the "lucky" order:

- first orientation signal increments to `1`
- then panel-orientation management decrements back to `0`

In the broken session, the opposite ordering happened:

- panel-orientation management decremented from `0` to `-1`
- no first orientation signal ever arrived afterward
- `should_claim` stayed false forever because the counter never returned to `0`

That ordering exactly matches the core:

- `panel_orientation_managed = true`
- `initial_orient_change_done = 0`
- `inhibited_count = -1`
- `iio_proxy != NULL`
- `has_accel = 1`
- `should_claim = 0`
- `is_claimed = 0`

So the most precise current diagnosis is:

- Mutter's auto-rotate claim path is startup-order-dependent.
- On this broken session, `update_panel_orientation_managed()` won the race
  before the first sensor-active/orientation event.
- That drove the inhibit counter negative and permanently suppressed
  `ClaimAccelerometer` for the rest of the session.

Most likely concrete callback chain:

1. `MetaOrientationManager::iio_proxy_ready()` runs.
2. It calls `update_has_accel()`.
3. `update_has_accel()` sets `has_accel = true` and emits
   `notify::has-accelerometer`.
4. `MetaMonitorManager` is connected to that notify signal, so
   `update_panel_orientation_managed()` runs immediately.
5. Because touch mode, builtin panel, and accelerometer are all now true,
   `update_panel_orientation_managed()` flips panel orientation to managed and
   calls `meta_orientation_manager_uninhibit_tracking()`.
6. At this point no prior matching inhibit has happened yet, so the counter goes
   from `0` to `-1`.
7. Control returns to `iio_proxy_ready()`, which only _then_ calls
   `sync_accelerometer_claimed()`.
8. `sync_accelerometer_claimed()` sees `inhibited_count == -1`, computes
   `should_claim = false`, and never sends `ClaimAccelerometer`.

That exact chain is not yet live-traced from a fresh login, but it is the best
fit to both:

- the source ordering in `iio_proxy_ready()`
- and the captured broken-session core state

## Package / Regression Notes

The user reports this worked in the past, including within roughly the last
three months.

Older retained local generations (`system-78`, `system-95`) and the current one
(`system-96`) all point at the same `mutter-49.2` store path:

- `/nix/store/n6qcqizvig95j7839fh1s3qm71wgvjsx-mutter-49.2`

They also point at the same `gnome-settings-daemon-49.1` build.

This means the earlier "likely regression between `2026-04-18` and
`2026-04-22`" theory is no longer supported by package evidence alone.

Updated interpretation:

1. The root cause is very likely a race/order bug already present in the
   installed `mutter-49.2` build.
2. If the user truly saw auto-rotate work on one of these retained generations,
   that would fit an intermittent startup-order race rather than a clean package
   upgrade regression.
3. A boot test into an older generation is still useful as a behavioral check,
   but not because those retained generations use a different Mutter build.

## Upstream provenance

The ordering-dependent logic was introduced upstream in Mutter commit:

- `9bed859ad` - `backend: Inhibit orientation sensor when panel orientation is not managed`
  - author date: `2024-11-11`
  - merged upstream: `2025-08-28`
  - merge request referenced by the commit message: `GNOME/mutter!4119`

That commit changed `MetaMonitorManager` to:

1. call `meta_orientation_manager_inhibit_tracking()` on the first
   `orientation-changed` path
2. call `meta_orientation_manager_uninhibit_tracking()` when panel orientation
   becomes managed
3. call `meta_orientation_manager_inhibit_tracking()` when panel orientation
   becomes unmanaged

The underlying inhibit API came from earlier commit:

- `dc7eca63c` - `orientation-manager: Add API for inhibiting orientation change listening`

As of the fetched current upstream `main`, the same `inhibited_count` logic and
the same unguarded decrement are still present. No follow-up fix for the
negative-counter startup race was found in the inspected history.

## Next Steps

The immediate next step is to activate the host-local Mutter patch on `rugged`
and test the first fresh GNOME session after the switch.

### Post-switch validation checklist

1. Run:

```bash
sudo nixos-rebuild switch --flake .#rugged
```

2. Start a fresh graphical session:
   - log out and back in, or reboot
   - a fresh session is important because the running `gnome-shell` process must
     load the patched Mutter libraries
3. Before running `monitor-sensor`, `busctl monitor`, manual D-Bus claims, or any
   other sensor-debug tooling, test auto-rotate normally:
   - ideally with no external monitor attached for the first check
   - enter tablet mode
   - rotate the device and hold each orientation for `2-3s`
4. Expected success criteria:
   - the built-in display rotates on its own
   - GNOME no longer needs another client to claim the accelerometer first

### First post-patch outcome

- After `nixos-rebuild switch` and a fresh sign-out/sign-in, auto-rotate worked
  again in normal use.
- This is consistent with the local Mutter patch fixing the negative
  `inhibited_count` startup wedge.
- However, it is only one fresh-session trial so far. Because the suspected root
  cause is a startup-order race, one success does **not** prove the patch is the
  only reason it worked; a flaky unpatched session could also have come up in a
  lucky order.

Current confidence:

1. The patch is plausible and is not obviously wrong.
2. The original diagnosis is still the best fit to the captured broken-session
   evidence.
3. Repeated fresh-session tests are still needed before treating the fix as
   confirmed.

### If the patch works

1. Confirm it is stable across:
   - one logout/login cycle
   - one reboot
2. Re-test with the external monitor attached if that setup matters.
3. Use the result to prepare:
   - an upstream bug report with the captured RCA
   - and, if appropriate, an upstream patch based on the local fix

### If the patch does not work

1. Verify the patched system is actually active:
   - confirm the switch completed successfully
   - confirm the test was done in a fresh GNOME session after the switch
2. Capture one fresh failure from the patched session before using sensor tools:
   - check whether auto-rotate is still dead immediately after login
3. If still broken, return to deep instrumentation on the fresh patched session:
   - inspect `gnome-shell` state again with `gdb` / core dump
   - specifically check whether `inhibited_count` is still negative
   - and whether `panel_orientation_inhibit_tracking` reflects the intended new
     ownership logic
4. If the patched session still reaches a bad state, the next branch is either:
   - another startup-order path not covered by the current fix
   - or a separate issue downstream of sensor claiming

At this point, a pure host-config fix is no longer the interesting question. The
main question is whether the host-local Mutter patch eliminates the negative
`inhibited_count` startup wedge in a fresh session.

## Local Mitigation

A `rugged`-only Nix overlay patch was added to locally patch Mutter on this host:

- host config: `nix/nixos/hosts/rugged/default.nix`
- patch file: `nix/nixos/hosts/rugged/mutter-auto-rotate-startup-race.patch`

Patch intent:

1. Track whether `MetaMonitorManager` itself currently owns a
   panel-orientation inhibit.
2. Do not `uninhibit` when panel orientation becomes managed unless that exact
   inhibit was previously taken.
3. Do not take the one-shot "first orientation measurement" inhibit if panel
   orientation is already managed by the time the first sensor event arrives.
4. Add a defensive guard in `MetaOrientationManager` so unmatched
   `uninhibit_tracking()` calls cannot drive `inhibited_count` negative.

Validation:

- `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`
  completed successfully with the host-local patch in place.
- The patched `mutter-49.2` and rebuilt `gnome-shell-49.2` both built
  successfully as part of that closure.

## Second Incident — Auto-rotate wedged in vertical (2026-05-08)

After several days of normal operation through multiple suspend/resume cycles,
auto-rotate again stopped working. The user reported the display wedged in
portrait orientation. The patched Mutter (now `49.4`, with the same patch
re-applied via the overlay — confirmed by the `Ignoring unmatched orientation
tracking uninhibit` warning string being present in
`libmutter-17.so`) is doing its job; this is a different failure mode.

### Symptoms

- `gnome-shell` user journal shows recurring
  `Failed to claim accelerometer: Timeout was reached`
  and `gsd-power[…]: Claiming light sensor failed: Timeout was reached`
  paired with each post-resume claim attempt
  (e.g. `2026-05-07T18:05`, `2026-05-07T23:39`, `2026-05-08T14:11`,
  `2026-05-08T19:56`, all immediately following `PM: suspend exit`).
- `iio-sensor-proxy` is running but `busctl call … ClaimAccelerometer` times
  out, even though property reads (`HasAccelerometer`,
  `AccelerometerOrientation`) succeed.
- No `Ignoring unmatched orientation tracking uninhibit` warnings in the
  user journal — the negative-`inhibited_count` startup race is **not**
  recurring.

### Root cause: kernel-side IIO buffer/trigger wedge

Inspecting the live broken state:

- Kernel HID-sensor path is healthy:
  `/sys/bus/iio/devices/iio:device4/in_accel_*_raw` returns live values
  (e.g. `x=333425`, `y=5023`, `z=-928740` — strong gravity vector).
- But triggered-buffer mode is wedged:
  - `iio:device3` (als): `buffer/enable=1`, `current_trigger=als-dev3`
  - `iio:device4` (accel_3d): `buffer/enable=1`, `current_trigger=accel_3d-dev4`
  - `read(/dev/iio:device3)` and `read(/dev/iio:device4)` return `EAGAIN`
    indefinitely — buffer is empty, no trigger pushes data.
- `iio-sensor-proxy` thread stacks all in benign `ppoll` waits. `strace`
  on the live process shows it in a periodic loop every ~700ms:
  `openat /dev/iio:deviceN → read EAGAIN → close`.
- No HID-sensor or trigger errors in `dmesg` across the resume cycles.

So the underlying HID-ISHTP sensor still answers polled `_raw` reads, but
the trigger→buffer machinery silently stopped delivering samples after one
of the suspend/resume cycles. `iio-sensor-proxy` keeps polling for samples
that never come, so every `ClaimAccelerometer` call past that point times
out.

### Recovery (confirmed working)

```bash
systemctl stop iio-sensor-proxy.service
# Stopping the daemon already auto-disables both buffers via the kernel's
# release path; the explicit writes are belt-and-suspenders.
echo 0 > /sys/bus/iio/devices/iio:device3/buffer/enable
echo 0 > /sys/bus/iio/devices/iio:device4/buffer/enable
systemctl start iio-sensor-proxy.service
```

After this, `ClaimAccelerometer` succeeds immediately and
`AccelerometerOrientation` updates live (e.g. `"normal"` → `"left-up"`).
Mutter then claims the sensor on the next session-side trigger and
auto-rotate resumes working without a re-login.

### Investigation log (appended after deeper userspace dive)

The summary above was written with partial information. After reading the
iio-sensor-proxy 3.8 source and re-checking live state, several earlier
assertions need to be walked back. The following section catalogs raw
observations vs. inferences vs. open questions, so the next person picking
this up does not inherit assumptions that are not actually established.

#### Raw observations

Userspace / D-Bus:

- Mutter `49.4` is loaded with the auto-rotate-startup-race patch
  (`Ignoring unmatched orientation tracking uninhibit` warning string is
  present in `libmutter-17.so`).
- No `Ignoring unmatched orientation tracking uninhibit` warnings in the
  user journal during the incident. The earlier `inhibited_count`
  startup race is therefore not the cause of this incident.
- Recurring user journal entries paired with each `PM: suspend exit`:
  - `gnome-shell[…]: Failed to claim accelerometer: Timeout was reached`
  - `gsd-power[…]: Claiming light sensor failed: Timeout was reached`
- During the wedged state, `busctl --system get-property
net.hadess.SensorProxy ... HasAccelerometer AccelerometerOrientation`
  returned `b true` and `s "normal"` quickly. (This was tested before
  attaching strace; it does not establish that the daemon was generally
  servicing D-Bus.)
- During the wedged state, `busctl --system call ... ClaimAccelerometer`
  with strace attached did not produce any visible method-call recvmsg
  in the daemon's gdbus thread within 6s. The only D-Bus traffic
  observed during that window was a `NameOwnerChanged` corresponding to
  the test client's own disconnect.

Daemon process state during wedge:

- `iio-sensor-proxy` PID 1185, started 2026-05-05 22:24, alive through
  many suspend/resume cycles.
- 9 file descriptors. None point to `/dev/iio:device*`.
  Two unix sockets (one to the system bus), two eventfds, three further
  unix sockets / eventfds. stdin/stdout/stderr accounted for.
- All four threads (`iio-sensor-prox`, `pool-spawner`, `gmain`, `gdbus`)
  in benign waits (`ppoll` or `futex`). No thread blocked in a syscall
  on a sensor file.
- strace of TID 1185 (gmain) showed a periodic 700 ms cycle:
  `openat(/dev/iio:device3, O_RDONLY|O_NONBLOCK)` →
  `read(...) = -1 EAGAIN` → `close()`. Same for `/dev/iio:device4`.
  This pattern matches exactly what
  `drv-iio-buffer-accel.c::prepare_output()` does on EAGAIN.

Kernel sysfs / IIO state during wedge:

- `/sys/bus/iio/devices/iio:device3` (`als`):
  `buffer/enable=1`, `trigger/current_trigger=als-dev3`,
  `in_intensity_sampling_frequency=10.000000`.
- `/sys/bus/iio/devices/iio:device4` (`accel_3d`):
  `buffer/enable=1`, `trigger/current_trigger=accel_3d-dev4`,
  `in_accel_sampling_frequency=10.000000`.
- All seven IIO triggers present and named as expected
  (`accel_3d-dev4`, `als-dev3`, `gyro_3d-dev5`,
  `relative_orientation-dev2`, etc.).
- Direct sysfs polled reads on `iio:device4` were live:
  `in_accel_x_raw=333425`, `_y=5023`, `_z=-928740` — strong gravity
  vector on z (device roughly flat).
- `dmesg` since boot contains no `hid-sensor-*`, `hid_sensor_*`, or
  `iio` errors / warnings around any suspend/resume cycle. The only
  startup notes are the harmless `Not a switch [...]` and
  `Invalid bitmask entry for [.../input14/event13]` messages emitted
  once at 2026-05-05 22:24 and never repeated. No
  `hid_field_extract() called with n (192) > 32!` repeats either.

Recovery sequence:

- `systemctl stop iio-sensor-proxy.service` caused
  `iio:device3/buffer/enable` and `iio:device4/buffer/enable` to flip
  from `1` to `0` before the explicit `echo 0 > buffer/enable` writes
  ran. (The kernel auto-disabled the buffer on the daemon's exit.)
- `systemctl start iio-sensor-proxy.service` produced these new
  daemon-side log lines (from `is_buffer_usable()`):
  `Buffer '/dev/iio:device4' did not have data within 0.5s` and
  `Buffer '/dev/iio:device3' did not have data within 0.5s`.
- After the restart: `ClaimAccelerometer` returned successfully;
  `AccelerometerOrientation` updated to `"left-up"`; `monitor-sensor`
  reported live light readings (`145.127`, `145.845`, `146.680` lux,
  …) and live accel orientation. Mutter resumed normal auto-rotate.

Post-recovery sysfs / IIO state (with daemon running, idle, then
during a fresh `Claim`):

- All `scan_elements/*_en = 0`.
- `trigger/current_trigger = ""` (empty).
- `buffer/enable = 0`.
- These values **did not change** during a `ClaimAccelerometer` call.
- The daemon's fd table during the claim contained no
  `/dev/iio:device*` entries.

Daemon log at first boot (2026-05-05 22:23-24):

- `Started IIO Sensor Proxy service.`
- `Not a switch [...]`
- `Invalid bitmask entry for [...]`
- **No `Buffer '...' did not have data within 0.5s` warnings.**

Source-code observations (`iio-sensor-proxy 3.8`):

- `iio_buffer_accel_discover()` calls `iio_buffer_accel_open()` which
  calls `buffer_drv_data_new()`. `buffer_drv_data_new()` runs
  `iio_fixup_sampling_frequency`, `enable_sensors(device, 1)` (writes
  `1` to every `scan_elements/*_en`), `enable_trigger()` (writes
  `current_trigger`), `enable_ring_buffer()` (writes `buffer/length=128`
  and `buffer/enable=1`), and `build_channels()`. The buffer is then
  re-enabled and `is_buffer_usable()` polls 500 ms for `POLLIN`. If no
  data, the discover returns FALSE and the buffered driver is rejected
  for that device.
- After discovery, `buffer_drv_data_free()` runs and writes `0` to
  `buffer/enable`, `NULL` to `current_trigger`, and `0` to every
  `scan_elements/*_en`. So a rejected discover leaves nothing armed.
- The polled driver (`drv-iio-poll-accel.c`) reads
  `in_accel_{x,y,z}_raw` via uncached sysfs reads on a 700 ms timer.
  It does not touch `buffer/`, `trigger/`, or `scan_elements/`.
- `iio_poll_accel_set_polling(TRUE)` calls `poll_orientation()`
  synchronously once, so a fresh `Claim` on the polled driver always
  triggers `accel_changed_func()` immediately, which calls
  `maybe_notify_sensor_startup_finished()` and returns the delayed
  D-Bus invocation.
- `iio_buffer_accel_set_polling(TRUE)` similarly calls
  `read_orientation()` synchronously once. But `read_orientation()` ->
  `prepare_output()` returns early on `read=EAGAIN` without calling
  `callback_func`, so no `maybe_notify_sensor_startup_finished()` runs.
- `handle_generic_method_call()` for `Claim*`: when the first client
  for a sensor type arrives, it adds the invocation to
  `sensor_startup_dbus_invocations_delayed[type]` and starts polling.
  Subsequent clients during the "starting up" window are also added to
  the delayed list. The list is drained by
  `maybe_notify_sensor_startup_finished()` from the per-sensor
  `*_changed_func()` callback.
- There is no timeout on the delayed list. There is no
  retry/recovery on persistent EAGAIN.

#### Inferences supported by those observations

1. The wedged daemon was running the **buffered** driver for both
   `als` and `accel_3d`. The strace pattern is unique to that driver,
   and `buffer/enable=1` plus a non-empty `current_trigger` is what
   that driver leaves in sysfs while polling.
2. The kernel was producing no triggered samples in the wedged state.
   The buffer was armed (sysfs says so), the sampling frequency was
   set, the trigger was wired, but reads from `/dev/iio:deviceN`
   returned EAGAIN indefinitely.
3. The wedge is not a daemon-only state. After a clean daemon restart
   that re-runs `enable_sensors → enable_trigger → enable_ring_buffer`
   from scratch, `is_buffer_usable()` still does not see data within
   500 ms. The kernel/firmware is the layer that stopped delivering.
4. The daemon's "no timeout on first reading" path is what makes the
   wedge user-visible as a permanent claim hang. Without that
   amplification, a transient absence of samples would not stick.
5. Once the buffered discover failed, the daemon transparently fell
   back to the polled driver, which uses sysfs `_raw` reads. That path
   does not exercise the wedged kernel machinery and works fine.
   This is why the recovery "worked" without fixing the kernel side.
6. Polled-mode `Claim` has no equivalent hang because the synchronous
   first reading is taken directly from `_raw` and always succeeds.

#### Speculations / hypotheses (not established)

S1. The kernel HID-sensor data-ready trigger silently stops firing
after some s2idle resume. Plausible because the underlying ISH
polled path stays alive and the IIO sysfs view of the trigger
still looks normal. No direct evidence yet.

S2. The sensor of failure is per-sensor-type, not global: both `als`
and `accel_3d` are wedged together, but the kernel paths involved
are mostly shared (hid-sensor-trigger, hid-sensor-iio-common,
intel-ish-hid). A wedge in shared code would naturally take both
out simultaneously.

S3. The wedge requires a client to hold a claim across the suspend.
Every observed wedge correlates with `gsd-power` keeping the light
sensor claim active around the clock. Untested.

S4. There may be a race between iio-sensor-proxy's existing claim and
a parallel runtime-PM transition on the HID sensor at resume time
(e.g., post-resume `_hid_sensor_power_state(false)` then later
`(true)` with the IIO buffer already enabled, leaving an
inconsistent state).

S5. The 2026-05-05 boot saw buffered discover succeed; later the same
daemon process was wedged on the buffered driver. So the buffer
worked at least once on this kernel. The wedge is not a permanent
"this hardware never supports buffered mode" condition.

S6. Polled mode is not strictly worse than buffered mode for this
workload — both run on a 700 ms timer; sample rate is the same.
So forcing the daemon to polled would be a pragmatic sidestep
of (S1) without functional regression. Untested whether
`gsd-power` light readings are equivalent in polled mode for
`als` (the `als` polled driver path was not examined).

#### Open questions

Q1. Did `accel_3d` ever wedge before `als`, or vice versa, or only
together? Check by separately monitoring buffer state of each
device across suspend cycles.

Q2. Does toggling only `buffer/enable=0/1` (without
`enable_sensors`/`enable_trigger`) un-wedge the kernel state? If
yes, the wedge is in the buffer-arm path; if no, it is deeper
(in the trigger or HID-sensor PM ops).

Q3. Does the wedge require a client to hold a claim across suspend?
Test by killing all SensorProxy clients before suspend, resuming,
and probing.

Q4. Is `gyro_3d`, `magn_3d`, `dev_rotation`, `incli_3d`, or
`relative_orientation` ever in the wedged state too? They are
served by the same hid-sensor-trigger code path. The daemon
does not claim them, so we would not notice.

Q5. Which suspend cycle introduced the wedge, and what was different
about that cycle vs. the preceding ones that did not wedge?

Q6. Does the wedge survive a `rmmod hid_sensor_*; modprobe …` cycle?
That would tell us whether the bad state is in the kernel module
or in the ISH firmware.

Q7. Does `/sys/bus/iio/devices/iio:device4/buffer/data_available`
(bytes currently in the kernel buffer) read 0 in the wedged
state? That would confirm "the buffer is empty", as opposed to
"data is in the buffer but the daemon's read does not see it".

Q8. Is there visible activity on the ISH IPC channel during the
wedge? Counter / ring-buffer in `intel-ish-hid` / `mei` debugfs
might show whether messages are arriving from the firmware.

#### Components / paths potentially involved

Userspace:

- `iio-sensor-proxy 3.8`
  - `iio-sensor-proxy.c::handle_generic_method_call`,
    `client_release`, `client_vanished_cb`,
    `maybe_notify_sensor_startup_finished`, `accel_changed_func`,
    `light_changed_func`
  - `drv-iio-buffer-accel.c::iio_buffer_accel_discover`,
    `_open`, `_set_polling`, `read_orientation`, `prepare_output`
  - `drv-iio-buffer-light.c` (analogous)
  - `drv-iio-poll-accel.c::iio_poll_accel_set_polling`,
    `poll_orientation`
  - `iio-buffer-utils.c::buffer_drv_data_new`, `buffer_drv_data_free`,
    `enable_sensors`, `enable_trigger`, `disable_trigger`,
    `enable_ring_buffer`, `disable_ring_buffer`,
    `is_buffer_usable`
- D-Bus broker (`dbus-daemon` system bus)
- Clients: `gnome-shell` (Mutter), `gsd-power`, `monitor-sensor`

Kernel — IIO core:

- `drivers/iio/buffer/industrialio-buffer-cb.c`,
  `industrialio-triggered-buffer.c`, `kfifo_buf.c`
- `drivers/iio/industrialio-trigger.c`
- `drivers/iio/industrialio-buffer.c`
- `drivers/iio/inkern.c`
- `drivers/iio/industrialio-core.c` (chrdev `read`, sysfs)

Kernel — HID sensor:

- `drivers/iio/common/hid-sensors/hid-sensor-trigger.c`
  - `hid_sensor_data_rdy_trigger_set_state`
  - `hid_sensor_setup_trigger`
  - `_hid_sensor_power_state`
  - `hid_sensor_runtime_resume`, `hid_sensor_runtime_suspend`
  - The `pm_ops` table on the IIO device
- `drivers/iio/common/hid-sensors/hid-sensor-attributes.c`
- `drivers/iio/common/hid-sensors/hid-sensor-iio-common.c`
- `drivers/iio/accel/hid-sensor-accel-3d.c`
  - `accel_3d_proc_event`, `accel_3d_capture_sample`
- `drivers/iio/light/hid-sensor-als.c` (analogous)

Kernel — HID transport:

- `drivers/hid/hid-sensor-hub.c`
  - `sensor_hub_get_feature`, `sensor_hub_set_feature`
  - `sensor_hub_input_get_attribute_info`, `_event`,
    `_raw_event`
- `drivers/hid/intel-ish-hid/`
  - `ipc/`, `ishtp/`, `ishtp-fw-loader.c`
  - `ishtp-hid-client.c`, `ishtp-hid.c`

Kernel — PM:

- `kernel/power/suspend.c` (s2idle path)
- `drivers/base/power/runtime.c`
- platform / chipset s2idle hooks for Intel Lunar Lake

Firmware:

- Intel ISH firmware itself (proprietary; no source). Loaded via
  `intel/ish/ish_lnl_b0.bin` or similar. Possibly relevant: ISH
  firmware version, ISH reset state across s2idle.

#### Instrumentation menu

What we can capture at increasing levels of invasiveness. Items
labeled "no rebuild" can be enabled at runtime; "module reload"
needs `rmmod`/`modprobe`; "kernel rebuild" needs custom kernel.

Userspace, no rebuild:

- `journalctl --user -u org.gnome.Shell` and `gnome-session.target`
  on every resume. We have this already.
- Run `iio-sensor-proxy` with `G_MESSAGES_DEBUG=all` (override the
  systemd unit's `Environment=`). This unlocks every `g_debug` line
  in the daemon — including `Accel read from IIO`, `No new data
available on '...'`, `Already enabled sensor`, claim-handler
  traces, etc. Cheap and high-signal.
- `busctl monitor --system net.hadess.SensorProxy` continuously
  during a test cycle, to see the exact `Claim` / `Release` traffic
  and replies from the daemon.
- `strace -fp <iio-sensor-proxy-pid>` long-running into a file.
  High overhead but complete.
- `gdb -p <iio-sensor-proxy-pid>` with breakpoints on
  `read_orientation`, `prepare_output`, `accel_changed_func`,
  `maybe_notify_sensor_startup_finished`. Inspect
  `data->sensor_startup_dbus_invocations_delayed[i]->len`,
  `data->clients[i]` size, and `drv_data->buffer_data->scan_size`.
- Periodic watchdog probe: do a brief
  `Claim → wait 1 s → Release` cycle every 60 s. Record
  timestamp, latency, whether the property changed. First failed
  probe pinpoints the wedging cycle to within 60 s.

Kernel, no rebuild — dynamic debug:

- `echo 'module hid_sensor_trigger +pmf' >
/sys/kernel/debug/dynamic_debug/control` (and similarly for
  `hid_sensor_iio_common`, `hid_sensor_hub`, `hid_sensor_accel_3d`,
  `hid_sensor_als`, `industrialio`, `intel_ish_ipc`,
  `intel_ishtp_hid`).
  - Every `pr_debug` in those modules will then go to dmesg.
  - Particularly useful inside `_hid_sensor_power_state` and
    `hid_sensor_data_rdy_trigger_set_state`, which are the
    PM-related state transitions on the trigger.
- Set this from a NixOS module so it persists across reboots
  (kernel cmdline `dyndbg=...` or systemd-tmpfiles writing the
  control file at boot).

Kernel, no rebuild — ftrace:

- `trace-cmd record -e power:* -e iio:* -e hid:* sleep N` across a
  suspend cycle. Captures suspend/resume sequencing alongside any
  HID / IIO tracepoints (limited set; mostly `power`).
- Function tracer on a focused list:
  `hid_sensor_data_rdy_trigger_set_state`, `_hid_sensor_power_state`,
  `hid_sensor_runtime_resume`, `hid_sensor_runtime_suspend`,
  `hid_sensor_get_report`, `hid_sensor_capture_sample`,
  `iio_push_to_buffers`, `iio_push_to_buffers_with_timestamp`,
  `iio_buffer_chrdev_read`, `iio_trigger_notify_done`.
  ```bash
  echo function > /sys/kernel/tracing/current_tracer
  echo 'hid_sensor_*' > /sys/kernel/tracing/set_ftrace_filter
  echo 'iio_push_to_buffers*' >> /sys/kernel/tracing/set_ftrace_filter
  echo 1 > /sys/kernel/tracing/tracing_on
  ```
- `function_graph` for the same list — much heavier but shows the
  full call chain.

Kernel, no rebuild — bpftrace / kprobes:

- Count callbacks in `iio_push_to_buffers_with_timestamp` per second
  (silver-bullet — if it stops, the trigger has stopped).
  ```
  bpftrace -e 'kprobe:iio_push_to_buffers_with_timestamp { @[comm] = count(); } interval:s:5 { print(@); clear(@); }'
  ```
- Trace enter/exit of `hid_sensor_data_rdy_trigger_set_state` to see
  who is toggling the trigger and whether it succeeds:
  ```
  bpftrace -e 'kprobe:hid_sensor_data_rdy_trigger_set_state { printf("%llu set_state arg=%d caller=%s\n", nsecs, arg1, kstack); }'
  ```
- Similar on `_hid_sensor_power_state` and the HID-sensor hub's
  `sensor_hub_set_feature` to see whether report-enable writes go
  out at the expected times.

Kernel sysfs / debugfs polling:

- Repeatedly read
  `/sys/bus/iio/devices/iio:device4/buffer/data_available` while
  wedged — that tells us how many bytes are queued in the kernel
  buffer right now, distinguishing "trigger fires but no consumer"
  from "trigger never fires".
- `/sys/kernel/debug/iio` (if present) may expose internal state.
- `/sys/kernel/debug/intel_ishtp/*` or `/sys/class/mei/mei0/*` for
  ISH IPC state and counters.

Suspend bracketing:

- `/usr/lib/systemd/system-sleep/` (under NixOS:
  `system.systemSleep.preSleep`, `postResume`) hook scripts that on
  every suspend/resume snapshot:
  - Buffer/trigger/scan_elements state of `iio:device3` and
    `iio:device4`.
  - `data_available` counter.
  - dmesg cursor.
  - `busctl get-property AccelerometerOrientation` (cached value).
  - List of holders of `net.hadess.SensorProxy` claims.
- Output to `/var/log/iio-bracket/<timestamp>.log`. The first `post`
  log to record `data_available=0` _and_ a stale orientation
  identifies the wedging cycle. Compare its dmesg-since-pre against
  a working cycle's.

Reproducer harness:

- `rtcwake -s 60 -m mem` in a loop with the buffered daemon active,
  alternating with monitor-sensor probes. If we can repro on demand,
  we can A/B kernels, modules, ISH firmware versions, and patches.
- A second variant where we toggle whether a client holds a claim
  across the suspend (Q3).

Module reload (mid-invasive):

- Reload `hid_sensor_accel_3d`, `hid_sensor_als`,
  `hid_sensor_trigger`, `hid_sensor_iio_common`, `hid_sensor_hub`,
  `intel_ishtp_hid`, `intel_ishtp` in the right order while wedged,
  then re-probe. If the wedge survives, the bad state is in the ISH
  firmware (or persistent in `industrialio` core); if it clears,
  the bad state was inside the reloaded modules.

Kernel rebuild:

- Add WARN_ON / printk inside `hid_sensor_data_rdy_trigger_set_state`
  and `iio_push_to_buffers_with_timestamp` if dynamic debug isn't
  rich enough.
- Apply `CONFIG_DEBUG_OBJECTS_TIMERS`, `CONFIG_DEBUG_OBJECTS_WORK`
  for unrelated coverage in case ISH workqueue items are corrupted.
- Build a kernel with explicit instrumentation in the HID-sensor PM
  resume path. Last resort.

#### Suggested first cut

1. Persist dynamic debug for `hid_sensor_*`, `industrialio`,
   `intel_ishtp_hid` from boot.
2. Add the system-sleep bracket hook.
3. Add the every-60s watchdog probe (claim → release).
4. Force `G_MESSAGES_DEBUG=all` on the daemon so the next wedge has
   verbose userspace traces too.
5. Do **not** auto-restart the daemon on resume yet. We want the
   wedge to be observable.

Wait for the next wedge. The bracket log + dmesg + watchdog log
together should be sufficient to either nail a kernel-side root
cause or rule one out, after which we can pivot to either an upstream
fix or a deliberate "force polled mode" host-config workaround.

## Status 2026-05-19

After multiple reboots and suspend/resume cycles, auto-rotate is
working correctly. IIO sensor proxy is running in **buffered mode**
(`iio:device5/buffer/enable=1`, `trigger=accel_3d-dev5`; confirmed via
`iio-sensor-proxy` journal showing `Accel read from IIO on
'iio:device5'`). The kernel-side IIO trigger wedge (second incident) has
not recurred in this session. The Mutter patch (`mutter-auto-rotate-startup-race.patch`) continues to be active and the negative-`inhibited_count` startup race has not been observed. User confirmed display is auto-rotating correctly.

### Live investigation 2026-05-08 (post-instrumentation switch)

After deploying `nix/nixos/hosts/rugged/iio-debug.nix`, we used the
verbose dyndbg + watchdog + journal trace to characterize the wedge
in detail. Key new observations:

#### Observation: the wedge is per-sensor, alternating

When iio-sensor-proxy starts, it runs `is_buffer_usable()` (500 ms POLLIN
test) per device. We observed two consecutive daemon starts with
opposite outcomes:

- **2026-05-08 20:54:** accel `did not have data within 0.5s` → polled.
  als `Found IIO buffer ALS` → buffered.
- **2026-05-08 21:19** (after `systemctl stop ; sleep 30 ; systemctl start`):
  accel `Found IIO buffer accelerometer` → buffered.
  als `did not have data within 0.5s` → polled.

So whichever sensor was _previously_ in polled mode becomes the wedged
one on the next restart, and the previously-wedged sensor recovers.

#### Observation: parent device's runtime PM state determines the outcome

Per-sensor parent device runtime_status during the test:

| Test phase                                  | accel parent       | als parent         |
| ------------------------------------------- | ------------------ | ------------------ |
| Wedged (mutter holding accel polled)        | active, usage=0    | suspended, usage=0 |
| 30s after `systemctl stop`                  | suspended, usage=0 | suspended, usage=0 |
| After restart (buffered accel + polled als) | active, usage=1    | suspended, usage=0 |

The pattern: a sensor whose parent is stuck `active` cannot get its
buffered path to work. The sensor whose parent is `suspended` arrives
at buffered mode cleanly.

#### Observation: `_hid_sensor_power_state` is NOT called when parent is `active`

With dyndbg `+pmf` on `hid_sensor_trigger` (the `_hid_sensor_power_state`
callsite), we counted hits during a manual `buffer/enable=1` write while
iio-sensor-proxy was running:

| Test                                                      | `_hid_sensor_power_state` | `hid_sensor_push_data` | data_available | parent runtime_status |
| --------------------------------------------------------- | ------------------------- | ---------------------- | -------------- | --------------------- |
| Manual arm accel (parent=active)                          | 0                         | 0                      | 0              | active                |
| Manual arm als (parent=suspended)                         | 1                         | 0 (different module)   | 6              | suspended             |
| Manual arm accel after 30s daemon stop (parent=suspended) | 1                         | 1                      | 1              | active during enable  |

The kernel skips writing the HID power-state feature to the firmware on
buffer enable when the runtime PM `pm_runtime_get_sync()` finds the
device is already active. ALS's enable goes through the suspend→resume
transition which fires the feature write. Accel's enable, with the
parent already active, does not.

The journal line on a working transition is:

```
hid_sensor_trigger:_hid_sensor_power_state: HID_SENSOR HID-SENSOR-200041 set power_state 2 report_state 2
hid_sensor_trigger:_hid_sensor_power_state: HID_SENSOR HID-SENSOR-200041 set power_state 6 report_state 1
```

(values 2/2 = D0 + report-all-events, 6/1 = D5/disable + no-events.)
On the failing transition there are zero such lines for HID-SENSOR-200073
(accel).

#### Observation: continuous Claim is what locks runtime PM `active`

mutter holds `ClaimAccelerometer` continuously while in tablet mode. In
the polled driver path this means the daemon's `iio_poll_accel` 700 ms
timer keeps reading `in_accel_*_raw`, each of which does
`pm_runtime_get_sync` → read → `pm_runtime_put_autosuspend`. With
`autosuspend_delay_ms=3000` and a 700 ms read interval, the autosuspend
timer is reset before it can fire and the parent stays `active`
indefinitely.

ALS, when in polled mode, does _not_ trip this lock-in: gsd-power's
light-sensor claim is sparse enough that the autosuspend timer fires
between claims.

So the "wedge" is anchored by mutter's continuous accel claim
specifically, plus the daemon's polled fallback for that sensor.

#### Mechanism — full chain

1. Some triggering event (suspect: a particular s2idle resume) causes
   the firmware's accel streaming subscription to drop while the kernel
   still believes the device is in streaming mode (`user_requested_state`
   counter > 0 in `_hid_sensor_power_state` or similar). After this,
   `hid_sensor_data_rdy_trigger_set_state(true)` thinks "no transition
   needed" and skips the feature write.
2. iio-sensor-proxy enters the read-EAGAIN polling loop on the
   buffered driver path. Without a first reading, the daemon never
   resolves its delayed Claim invocations. mutter's `Claim`
   times out; the user sees auto-rotate stop.
3. Stopping iio-sensor-proxy: kernel auto-disables the buffer, the
   daemon's userspace state is gone. But mutter's Claim is also gone
   in the process.
4. Re-starting iio-sensor-proxy: discovery's `is_buffer_usable()` runs
   the same arm-and-wait test. Same kernel skip behavior, same
   "did not have data within 0.5s", same fall-back to polled mode.
5. In polled mode, mutter re-claims accel. The daemon's polled timer
   reads sysfs continuously, keeping the parent `active` and the
   autosuspend timer perpetually reset. Buffered mode now cannot
   recover even on subsequent daemon restarts.
6. The OTHER sensor (als here), unburdened by a continuous claim,
   has a normally-cycling parent runtime PM state, so its buffered
   path keeps working. The wedge appears asymmetric from the user's
   side.

#### Reliable manual recovery (verified)

```bash
systemctl stop iio-sensor-proxy.service
# Wait > autosuspend_delay_ms (3 s) for the parent to suspend.
# 30 s is conservative and easy to script.
sleep 30
systemctl start iio-sensor-proxy.service
```

After this, daemon discovery sees `runtime_status=suspended` for the
accel parent, `is_buffer_usable()` arms the buffer, the kernel
transitions through the resume path, `_hid_sensor_power_state(true)`
fires, firmware starts streaming, data arrives within 500 ms,
buffered driver wins.

Caveat: the _other_ sensor (the one that was buffered before the stop)
will re-discover with its parent still `active` (because gsd-power may
still be claiming, or the discover for the first device kept its
parent up via cross-sensor state in the ISH device). Result: mode
flips between the two sensors on each restart cycle. For mutter
auto-rotate that doesn't matter — only accel needs to be functional —
but it does mean `als` may run polled until the daemon is bounced
again with a long pause.

#### Open kernel-side question

Why does the firmware's accel streaming sometimes drop while the
kernel's `_hid_sensor_power_state` accounting still believes it is on?
The candidates remain:

- Suspend/resume handling in `hid-sensor-trigger` does not clear or
  resync the per-device atomic counter on s2idle resume.
- An ISH firmware bug where the streaming subscription is silently
  dropped on a runtime PM transition that the kernel doesn't see.
- A race between mutter's `Claim` arriving as iio-sensor-proxy is in
  the middle of a state transition, leaving the counter and the
  firmware out of sync.

We have no direct evidence which it is. Future bracket logs for the
_originating_ suspend cycle (we never captured one yet — the
instrumentation has only been active since 20:54) should help.

#### Practical implications

1. The auto-rotate-wedge symptom is now well-understood and
   auto-detectable: the watchdog can record `state=ok mode=polled`
   with `n_ok > 0` for accel as the wedged signature, distinct from
   the original `state=WEDGE` (n_eagain spike) that we expected. The
   userspace daemon recovers itself into polled mode now, so the
   `WEDGE` state should not recur — the daemon's `Claim` no longer
   hangs, because polled-driver `Claim` returns immediately. The
   user-visible failure mode shifts from "auto-rotate dead" to
   "auto-rotate alive but slightly higher CPU".
2. A reliable recovery to buffered mode exists in userspace, scriptable.
3. The system-sleep bracket is still worth keeping to capture the
   originating event (the moment firmware streaming dies).
4. A `post-resume` hook that does the long-pause restart cycle would
   probably preempt the wedge entirely, at the cost of a brief
   auto-rotate gap on every resume. Reasonable tradeoff once we
   have one more observation cycle to confirm the model.
