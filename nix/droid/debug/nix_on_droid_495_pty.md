# nix-on-droid switch fails: getting pseudoterminal attributes: Permission denied

**Status: open, active investigation.** Device: `pixel6` (Pixel 6, aarch64, Android 14,
kernel `6.1.157-android14-...`).

## Symptom

`nix-on-droid switch --flake github:agentydragon/ducktape?ref=devel#pixel6` fails
deterministically (every attempt so far, both against `devel` and against test branches)
at the same activation step:

```
Activating installPackages
replacing old 'nix-on-droid-path'
installing 'nix-on-droid-path'
error: getting pseudoterminal attributes: Permission denied
error:
       … while waiting for the build environment for '.../user-environment.drv'
       to initialize (failed with exit code 1, previous messages: )
error: reading a line: Input/output error
```

The active `nix` on this device throughout has been the original bootstrap-era
`2.18.8` — no switch has ever completed to promote a newer one.

## Confirmed

- The error is Nix's own code, not proot's or bash's: `src/libstore/unix/build/
unix-derivation-builder.cc` (`openSlave()`) does `posix_openpt()` → fork →
  `open(slave)` → `tcgetattr()` unconditionally, on every local build, to capture
  builder stderr in raw mode. `tcgetattr()` is the call that fails.
- Deterministic: identical error, identical step, across every attempt.
- A bare `ls -la /dev/pts` also gets `Permission denied` despite permissive DAC bits
  (`/dev/pts` 755, `/dev/ptmx` 666 root:root) — but per the VM investigation below,
  this is a universal, years-old Android behavior (`search` without `read` on the
  devpts directory, for every app) and does **not** indicate a new or broader
  restriction. Noted here so nobody re-derives it as a lead.
- Executing real Android binaries through nix-on-droid's `-b /:/android` proot bind
  (`/android/system/bin/getenforce`, `.../toybox`) fails with `cannot execute:
required file not found` — likely their ELF interpreter living under `/apex/...`
  not being reachable through that bind. Closes off "just exec real Android tools
  through the proot" as a diagnostic path.

## Ruled out

- **nix-on-droid's build-users-group/nixbld privilege-drop dance.** nix-on-droid
  runs with no fakeroot at all (`login.nix`'s proot invocation has no `-0`), and its
  `/etc/passwd`+`/etc/group` only ever define `root` and one `nix-on-droid` user at
  the real UID — no `nixbld` group exists, so `useBuildUsers()` never engages.
- **Nix's own seccomp/landlock/NO_NEW_PRIVS setup.** Gated entirely behind
  `sandbox = true`; nix-on-droid hardcodes `sandbox = false`.
- **The nixpkgs regression in nix-community/nix-on-droid#495** (upstream, still open,
  no identified root cause there either). Tried pinning `pkgsAarch64` to a
  pre-regression nixpkgs revision (#7167) — identical failure with and without the
  pin (#7169, #7170 reverted it). Root cause of why it couldn't have worked: the
  _currently active_ `nix` binary drives the switch that would promote a newer one,
  so re-pinning `nix.package` can never affect the switch that's failing. Nix
  `2.18.8` also predates that regression window by over a year, so this device's
  failure isn't that regression anyway.

## Leading hypothesis: Android SELinux denying the ioctl, not Nix or proot

First VM reproduction attempt (x86_64, Android-x86 9, kernel 4.19 — chosen for setup
convenience, a real mistake: 5 Android versions and a different CPU arch away from
the actual device) found zero AVC denials on devpts across four variants, and read
the AOSP policy source directly: apps get their own pty type via `type_transition`
on `open()`, and that type's `allowxperm` ioctl range explicitly includes TCGETS.
That's a real, version-independent policy fact, but the empirical "nothing failed"
part of that run isn't good evidence given how far it was from the real device —
treat it as weak, not as a negative result.

A corrected VM attempt (aarch64, an Android version close to 14/kernel 6.1, real
guest network + proxy CA trust instead of routing around it, running the literal
`nix-on-droid switch --flake ...#pixel6`) is in progress. See
`debug/nix_on_droid_495_pty/` for the harness and detailed notes from both attempts.

## Open on-device checks (not yet reported back)

- `cat /sys/fs/selinux/enforce` (and `/android/sys/fs/selinux/enforce`) — enforcing
  vs permissive on the real device. Plain file read, no exec needed.
- AVC denials in logcat during an actual failing switch:
  `(timeout 60 /android/system/bin/logcat -b all > /tmp/logcat.txt 2>&1 &)`, then run
  the switch, then inspect the log. May hit the same exec-through-`/android` wall as
  `getenforce`.

## Related

- Upstream: nix-community/nix-on-droid#495 (open, unresolved).
- PRs: #7167 (nixpkgs pin, didn't fix it), #7169 (unrelated git-fetcher bugfix, kept),
  #7170 (revert of #7167).
- VM reproduction branch: `debug/nix-on-droid-495-android-vm-repro`.
