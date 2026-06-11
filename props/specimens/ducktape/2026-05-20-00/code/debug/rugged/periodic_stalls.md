# Rugged Periodic Stall Investigation

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
