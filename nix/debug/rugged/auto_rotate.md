# Rugged Auto-Rotate Investigation

Date: 2026-04-25
Host: `rugged`
Hardware: Dell Pro Rugged 12 Tablet RA02260

## First incident (2026-04-25): Mutter never claims the accelerometer

GNOME auto-rotate did not rotate the built-in display. Fixed upstream; see
"Upstream fix and validation".

Signature, to tell it from the second incident's kernel-side wedge:

- Host config (`hardware.sensor.iio.enable`), `iio-sensor-proxy`
  (`HasAccelerometer = true`) and the `Intel HID switches` tablet-mode switch
  were healthy. `monitor-sensor` reported correct orientation changes once it
  claimed the sensor.
- `org.gnome.Mutter.DisplayConfig` had `PanelOrientationManaged = true`, but the
  built-in transform stayed `0` and no `MonitorsChanged` fired. A direct
  `ApplyMonitorsConfig` rotated `eDP-1` fine.
- The system bus showed no `ClaimAccelerometer` or `ReleaseAccelerometer` from
  the live session, and `strace` of `gnome-shell` showed no
  `net.hadess.SensorProxy` traffic.
- Restarting `iio-sensor-proxy`, unplugging the external monitor and disabling
  all Shell extensions did not help. The `gsd-power` "Release of light sensors
  failed" warning ("Sensor claim not allowed") was judged unlikely to be the
  cause.

Root cause (Mutter 49.2). A `gcore` of the broken `gnome-shell`, read with
matching debug outputs, showed `MetaOrientationManager` with `has_accel = 1`,
`orientation_locked = 0`, `should_claim = 0`, `is_claimed = 0` and
`inhibited_count = -1`, and `MetaMonitorManagerPrivate` with
`initial_orient_change_done = 0`. `sync_accelerometer_claimed()` claims only at
a count of 0, and `meta_orientation_manager_uninhibit_tracking()` has no
lower-bound guard. The count most likely went `0 → -1` when `iio_proxy_ready()`
→ `update_has_accel()` → `notify::has-accelerometer` →
`update_panel_orientation_managed()` called `uninhibit_tracking()` before any
matching inhibit. That chain was read from source, not traced live from a fresh
login.

Retained generations `system-78`, `system-95` and `system-96` all used
`mutter-49.2` and `gnome-settings-daemon-49.1`: a startup-order race, not a
package regression.

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

## Upstream fix and validation

GNOME Mutter [MR !4962](https://gitlab.gnome.org/GNOME/mutter/-/merge_requests/4962)
added explicit ownership of the unmanaged-orientation inhibit. The pinned Mutter
50.6 includes this fix: it releases that inhibit only if it previously acquired
it. This addresses the startup ordering that produced the negative counter on
`rugged`. The former local patch and its defensive counter guard have been removed.

The original patched Mutter 49.2 build worked after one fresh sign-in, but that
one trial did not establish reliability for the startup race. Mutter 50.6 has
not yet been validated on `rugged`. After switching to it, test auto-rotation in
a fresh session, after a reboot, and with and without the external monitor.
Initially avoid `monitor-sensor` or other clients that claim the accelerometer.
If it fails, capture the session before using sensor tools and check the counter,
sensor claim, and kernel IIO state; the second incident below had a separate
kernel-side cause.

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

These are the hypotheses recorded before the later live investigation below;
they are not evidence of the originating kernel/firmware defect.

- **S1:** a data-ready trigger stops after s2idle while polled ISH reads and
  trigger sysfs still look normal; no direct trigger evidence.
- **S2:** simultaneous ALS/accel failure could involve their shared trigger,
  common-sensor or ISH code; the per-sensor/global scope was unproved.
- **S3:** a claim held across suspend may be required; continuous gsd-power ALS
  claims correlated with the observed wedges but were not a controlled test.
- **S4:** claim/resume may race runtime PM: power false → true with an already
  enabled IIO buffer could leave inconsistent state.
- **S5:** buffered discovery worked at the 2026-05-05 boot before the same daemon
  wedged; this is not hardware that never supports buffered mode.
- **S6:** both drivers use a 700ms timer, so forced polling might sidestep S1
  without a rate regression. ALS/gsd-power equivalence remained untested.

#### Open questions

- **Q1:** separately monitor ALS/accel buffers across suspend: which fails first,
  or do both fail together?
- **Q2:** toggle only buffer enable, without sensor/trigger setup; recovery would
  implicate buffer arming, failure leaves trigger/HID PM deeper in scope.
- **Q3:** remove all SensorProxy client claims before suspend, resume and probe;
  test whether holding a claim is required.
- **Q4:** check unclaimed gyro, magnetometer, device rotation, inclination and
  relative-orientation sensors sharing the trigger code for hidden wedges.
- **Q5:** identify the originating suspend cycle and compare it with working ones.
- **Q6:** reload HID sensor modules to narrow module versus persistent firmware
  state; preserve the reload outcome and residual IIO-core uncertainty.
- **Q7:** inspect `iio:device4/buffer/data_available` for empty queue versus queued
  data invisible to daemon reads.
- **Q8:** inspect ISH IPC/MEI counters or rings for firmware messages during a wedge.

#### Components / paths potentially involved

The source map below belongs to the May 2026 incident, including
`iio-sensor-proxy 3.8`; current kernel/firmware attribution remains unproved.

- Userspace: `iio-sensor-proxy.c` claim/release/client-vanish/startup callbacks
  (`handle_generic_method_call`, `client_release`, `client_vanished_cb`,
  `maybe_notify_sensor_startup_finished`, `accel_changed_func`, `light_changed_func`);
  buffered `drv-iio-buffer-{accel,light}.c` paths (`iio_buffer_accel_discover`,
  `_open`, `_set_polling`, `read_orientation`, `prepare_output`);
  `drv-iio-poll-accel.c::{iio_poll_accel_set_polling,poll_orientation}`;
  `iio-buffer-utils.c::{buffer_drv_data_new,buffer_drv_data_free,enable_sensors,
enable_trigger,disable_trigger,enable_ring_buffer,disable_ring_buffer,
is_buffer_usable}`. Clients are Mutter, gsd-power and monitor-sensor on the
  system D-Bus broker.
- IIO core: `drivers/iio/{buffer/industrialio-buffer-cb.c,buffer/industrialio-triggered-buffer.c,
buffer/kfifo_buf.c,industrialio-trigger.c,industrialio-buffer.c,inkern.c,
industrialio-core.c}` for callbacks, buffers, chrdev reads and sysfs.
- HID sensors under `drivers/iio/common/hid-sensors/`: `hid-sensor-trigger.c`
  (`hid_sensor_setup_trigger`, `hid_sensor_data_rdy_trigger_set_state`,
  `_hid_sensor_power_state`, `hid_sensor_runtime_resume`, `hid_sensor_runtime_suspend`),
  its IIO `pm_ops`, `hid-sensor-attributes.c` and `hid-sensor-iio-common.c`.
  Sensor drivers are `drivers/iio/accel/hid-sensor-accel-3d.c`
  (`accel_3d_proc_event`, `accel_3d_capture_sample`) and
  `drivers/iio/light/hid-sensor-als.c` (analogous).
- Transport/PM: `drivers/hid/hid-sensor-hub.c` (`sensor_hub_get_feature`,
  `sensor_hub_set_feature`, `sensor_hub_input_get_attribute_info`, `_event`,
  `_raw_event`); `drivers/hid/intel-ish-hid/{ipc,ishtp,ishtp-fw-loader.c,
ishtp-hid-client.c,ishtp-hid.c}`; `kernel/power/suspend.c`,
  `drivers/base/power/runtime.c` and Lunar Lake s2idle hooks. Firmware is proprietary
  `intel/ish/ish_lnl_b0.bin` or similar; retain version/reset state when comparing.

#### Instrumentation menu

The implemented baseline is [iio-debug.nix](../../nixos/hosts/rugged/iio-debug.nix):
verbose daemon logging, boot/module dynamic debug, system-sleep snapshots and a
60-second journal-based watchdog, with wedge dumps. It does not auto-recover or
perform the proposed Claim/Release watchdog below. The host currently disables
this high-volume mode after journal flooding caused desktop stalls
([host config](../../nixos/hosts/rugged/default.nix), [stalls report](stalls/README.md)).
These are capture options for an active investigation, not instructions to enable
all instrumentation permanently.

Runtime capture without rebuilding:

- GNOME user journal (`org.gnome.Shell`, `gnome-session.target`) on resume; daemon
  `G_MESSAGES_DEBUG=all` for successful reads, EAGAIN, already-enabled sensors and
  claim-handler messages; continuous `busctl monitor --system net.hadess.SensorProxy`
  for exact Claim/Release calls and replies. The baseline already covers journals.
- `strace -fp <daemon-pid>` records complete syscalls at high overhead. With GDB,
  break on `read_orientation`, `prepare_output`, `accel_changed_func` and
  `maybe_notify_sensor_startup_finished`; inspect delayed-startup invocation length,
  `data->sensor_startup_dbus_invocations_delayed[i]->len`, `data->clients[i]`
  and `drv_data->buffer_data->scan_size`.
- An optional active watchdog can Claim → wait 1s → Release every 60s, recording
  timestamp, latency and property change; it bounds the first bad cycle to a minute.
  Keep it distinct from the implemented journal-only watchdog.
- Dynamic debug uses `+pmf` for `hid_sensor_trigger`, `hid_sensor_iio_common`,
  `hid_sensor_hub`, `hid_sensor_accel_3d`, `hid_sensor_als`, `industrialio`,
  `intel_ish_ipc` and `intel_ishtp_hid`; persist via module/cmdline or boot debugfs
  configuration only for the capture. Focus on `_hid_sensor_power_state` and
  `hid_sensor_data_rdy_trigger_set_state` PM transitions.
- ftrace/trace-cmd: correlate `power:*`, available `iio:*`/`hid:*` events across
  suspend (tracepoints are limited, mostly power). Function tracing can target
  `hid_sensor_*`, `iio_push_to_buffers*`, runtime resume/suspend,
  `hid_sensor_get_report`, `hid_sensor_capture_sample`,
  `iio_buffer_chrdev_read` and `iio_trigger_notify_done`; function_graph is heavier.
- BPF/kprobes can count `iio_push_to_buffers_with_timestamp` calls by comm, printed
  every 5s, and inspect trigger set-state enter/exit, its requested state (`arg1`),
  return status and caller stack. Also trace `_hid_sensor_power_state` and
  `sensor_hub_set_feature` to correlate expected report-enable writes.
- Poll buffer `data_available` to help distinguish a firing trigger without a consumer
  from no producer. Inspect optional `/sys/kernel/debug/iio`,
  `/sys/kernel/debug/intel_ishtp/*` and `/sys/class/mei/mei0/*` state/counters.
- Sleep brackets compare buffer/trigger/scan state, queued bytes, dmesg cursor,
  cached orientation and claim holders before/after suspend. The implemented
  `/var/log/iio-bracket/` snapshots include state and log tails, but do not record
  claim-owner lists or a dmesg cursor. Compare the first stale orientation plus
  zero queued bytes with a working cycle, retaining those capture gaps.
- Reproduction: `rtcwake -s 60 -m mem` with the buffered daemon, interleaved sensor
  probes; separately vary a claim held across suspend (Q3). This enables A/B
  kernels, modules, firmware and patches if the failure becomes reproducible.

More invasive options: reload accel/ALS, trigger/common/hub and ISHTP modules in
dependency order, then re-probe. Recovery localizes state to reloaded components;
failure still leaves firmware or persistent IIO-core state possible. A custom
kernel can instrument trigger set-state and buffer push with WARN_ON/printk,
enable `CONFIG_DEBUG_OBJECTS_TIMERS`/`CONFIG_DEBUG_OBJECTS_WORK` for possible
workqueue corruption, or instrument HID-sensor PM resume as a last resort.

#### Suggested first cut

The historical first capture combined dynamic debug, sleep brackets, a watchdog
and verbose daemon logs, without automatic resume recovery. Preserve the wedge
while correlating bracket/dmesg/watchdog evidence; only then select an upstream
fix or deliberate polled-mode workaround. The later investigation below records
what actually ran and what remains unresolved.

## Status 2026-05-19

After multiple reboots and suspend/resume cycles, auto-rotate is
working correctly. IIO sensor proxy is running in **buffered mode**
(`iio:device5/buffer/enable=1`, `trigger=accel_3d-dev5`; confirmed via
`iio-sensor-proxy` journal showing `Accel read from IIO on
'iio:device5'`). The kernel-side IIO trigger wedge (second incident) has
not recurred in this session. The local Mutter patch was active at the time, and the negative-`inhibited_count` startup race had not been observed. User confirmed display was auto-rotating correctly.

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
