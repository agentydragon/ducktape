# nix-on-droid switch fails: getting pseudoterminal attributes: Permission denied

**Status: root cause confirmed; repair/workaround open.** Device: `pixel6` (Pixel 6,
aarch64, Android build property `17`, kernel
`6.1.157-android14-11-gbd23337e42e7-ab14791245`).

## Why this matters — the broader goal

This isn't about nix-on-droid for its own sake. The actual goal is phone usage data
(which apps are open, ideally URLs visited) flowing into the cluster's ActivityWatch
setup, the same way every other device already does: see
[cluster/docs/activitywatch/README.md](../../../cluster/docs/activitywatch/README.md)
for how that works today (one central `aw-server-rust`, each device runs a small
importer — `@ducktape_activitywatch//importer` — that pushes into it over a
bearer-gated write route, deduped and idempotent), and
`cluster/k8s/TODO.md`'s "Wire the phone into ActivityWatch" item, which this whole
effort is in service of.

nix-on-droid was chosen (over Tasker, forking the aw-android app, or Android's
built-in "Linux Terminal" VM) specifically so the phone's config lives in this repo
the same way `nix/home/` and `nix/nixos/` already do for desktops — reflashing or
replacing the phone should be "reinstall the app, run one command," not
reconstructing everything by hand — and because it shares the host's network/process
space (unlike a VM), which the eventual importer will need to reach aw-android's
`127.0.0.1`-bound local server.

The explicit plan (the user's own sequencing): a minimal nix-on-droid hello-world
bring-up first (`nix/droid/`, `pixel6.nix` — landed in #7114), then build the actual
`aw-importer`-equivalent "pseudocron" on top of it (age/sops for the write token, a
phone-specific age keypair added as a new recipient on
`activitywatch-write-token.sops.yaml`, scheduling via Termux:Boot +
`termux-job-scheduler` since nix-on-droid has no systemd). None of that second phase
has started. **This bug blocks it entirely**: `nix-on-droid switch` is the only
deployment mechanism for the phone's config, so until it works reliably, no further
package or service — including the eventual importer — can be added at all.

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
- Deterministic: identical error, identical step, across every attempt, including a
  completely fresh `nix-on-droid` reinstall (wiped app data, fresh bootstrap). Rules
  out stale/corrupted state from earlier failed switches as the cause — this is
  structural, not accumulated.
- A bare `ls -la /dev/pts` also gets `Permission denied` despite permissive DAC bits
  (`/dev/pts` 755, `/dev/ptmx` 666 root:root) — but per the VM investigation below,
  this is a universal, years-old Android behavior (`search` without `read` on the
  devpts directory, for every app) and does **not** indicate a new or broader
  restriction. Noted here so nobody re-derives it as a lead.
- Executing real Android binaries through nix-on-droid's `-b /:/android` proot bind
  fails every time: `getenforce`, `toybox` (`cannot execute: required file not
found`, likely their ELF interpreter living under `/apex/...` not being reachable
  through that bind) and `logcat` (`No such file or directory` via `timeout`).
  Closed off as a diagnostic path — three different real Android binaries, same
  wall; don't spend more effort trying a fourth.
- `cat /sys/fs/selinux/enforce` and `/android/sys/fs/selinux/enforce`: both
  `Permission denied` (file exists, unreadable) — expected for an unprivileged app
  context on stock Android, so this doesn't tell us enforcing vs permissive either
  way, same as the exec failures above.
- On 2026-09-17, the phone was connected over USB-C with debugging enabled. `adb
devices` eventually showed the authorized device (`1A251FDF6005LY`). External
  ADB access made it possible to inspect Android's state without going through
  nix-on-droid's restricted process environment.
- `adb shell getenforce` reported `Enforcing`. While reproducing the switch, an
  external `adb logcat -b all` capture recorded this AVC at the failure point:

  ```text
  09-17 14:26:08.166  6078 6078 W nix-env: avc: denied { ioctl } for
      comm="nix-env" path="/dev/pts/1" dev="devpts" ino=4
      ioctlcmd=0x542a
      scontext=u:r:untrusted_app_27:s0:c214,c257,c512,c768
      tcontext=u:object_r:untrusted_app_all_devpts:s0:c214,c257,c512,c768
      tclass=chr_file permissive=0 app=com.termux.nix
  ```

  The audit command component `0x542a` matches the Linux `TCGETS2`
  terminal-attributes ioctl. This is the exact PTY operation that fails as
  `getting pseudoterminal attributes: Permission denied`; it is an Android
  SELinux denial, not a Nix build-user or ordinary DAC-permissions failure.

- The same capture also recorded a `proot-static` denial for `{ search }` on a
  cgroup2 directory. That is separate process-environment noise; the
  `nix-env` denial on `/dev/pts/1` is the one directly correlated with the
  switch failure.

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

## Confirmed root cause: Android SELinux denies the PTY ioctl

The stock Android app domain `u:r:untrusted_app_27:s0` is not permitted to issue
the terminal-attributes ioctl against the app-labeled devpts node
`u:object_r:untrusted_app_all_devpts:s0`. Nix's `openSlave()` calls
`tcgetattr()` while setting up the local build slave, so the denial aborts the
local derivation before activation can complete.

This explains all of the observed behavior:

- The failure is deterministic and survives a fresh nix-on-droid installation.
- Changing the nixpkgs revision or the configured Nix package cannot help when
  the currently running bootstrap Nix is the process performing the switch.
- The `/dev/pts` DAC mode is not sufficient: SELinux rejects the ioctl after
  the file has been opened.
- The old Android-x86 VM result is not evidence against this diagnosis; it had
  a different Android release, kernel, and architecture, and produced no
  useful AVC signal. The real phone's enforcing policy is now the decisive
  observation.

The original in-app diagnostic paths are no longer the information bottleneck:
external ADB logcat supplied the decisive AVC. Pulling
`/sys/fs/selinux/policy` would add policy detail, but requires root and is not
needed to establish the cause.

## Remaining work

The remaining question is which repair is worth carrying upstream:

- Patch/upgrade Nix or nix-on-droid so a denied terminal-attribute ioctl is
  handled gracefully, or so local build logging does not require this PTY
  operation. This needs a focused source-level change and validation that build
  output and failure reporting remain usable.
- On a rooted/custom-policy phone, inspect the actual policy and test a narrowly
  scoped permission for the nix-on-droid app domain. The exact device policy
  should be checked before proposing an `allow` rule; the generic app-domain
  names above are evidence from this build, not a portable policy recipe.
- As a workaround, arrange for every required path to substitute from a trusted
  cache or use a remote builder. This may avoid the failing local derivation but
  does not fix the local PTY incompatibility and needs end-to-end validation.

## Related

- Upstream: nix-community/nix-on-droid#495 (open, unresolved).
- PRs: #7167 (nixpkgs pin, didn't fix it), #7169 (unrelated git-fetcher bugfix, kept),
  #7170 (revert of #7167).
- VM reproduction branch: `debug/nix-on-droid-495-android-vm-repro`.
