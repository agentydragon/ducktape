# Rugged Power Management

`rugged` uses `power-profiles-daemon`, surfaced through GNOME's power mode UI. The practical default should be
`balanced`, not `power-saver`.

## Recommended Use

- Use `balanced` most of the time.
- Switch to `power-saver` only when battery life, heat, or noise matters more than responsiveness.
- Switch to `performance` manually for games or heavy AC-powered work if `balanced` is not enough.

Check the current mode with:

```sh
powerprofilesctl get
powerprofilesctl list
```

Set the normal default back to balanced with:

```sh
powerprofilesctl set balanced
```

## What We Learned

`power-profiles-daemon` persists the selected profile in:

```text
/var/lib/power-profiles-daemon/state.ini
```

If that file says `Profile=power-saver`, GNOME will show Power Saver and the daemon will restore that profile after
restart. That is what made the tablet feel laggy: it was not currently forced by an active hold, and UPower still
reported AC power correctly.

`battery_aware=true` is useful, but it does not mean "performance on AC and power-saver on battery". On this Intel
pstate machine, the important automatic behavior is inside the `balanced` profile:

```text
balanced on AC      -> energy_performance_preference=balance_performance
balanced on battery -> energy_performance_preference=balance_power
```

Seeing `scaling_governor=powersave` is normal with Intel pstate. The more useful signal is:

```sh
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
```

On AC with `balanced`, this should normally be `balance_performance`.

## Non-goals

There is no GNOME or `power-profiles-daemon` setting that directly maps:

```text
AC      -> performance
battery -> power-saver
```

If exact AC/battery profile switching becomes important, use a real policy layer such as `auto-cpufreq`, TLP/tuned, or a
small local service that calls `powerprofilesctl` on UPower AC state changes. Until then, prefer the standard GNOME/PPD
path: keep `balanced` selected and use `power-saver` intentionally.
