# nix-on-droid switch fails: getting pseudoterminal attributes: Permission denied

**Status: open, active investigation.** Device: `pixel6` (Pixel 6, aarch64, Android 14,
kernel `6.1.157-android14-...`).

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
weak evidence about the policy, not about the phone.

Second attempt (aarch64, Google's real Android 14 arm64-v8a system image, SELinux
enforcing by default, guest proxy/CA trust actually solved rather than routed
around) got the real kernel booting — `6.1.23-android14-4`, the Pixel 6's own
generation — but hit two independent, structural blockers before the literal
`nix-on-droid switch` command could run at all:

1. **init segfaults immediately after the kernel boots.** Root-caused: Google only
   ships arm64 emulator images for hosts with real ARMv8.5+ CPU behavior (Apple
   Silicon's HVF); under QEMU's software emulation (TCG) the guest only gets a
   single ARMv8.0 core with no working multi-core boot path (`psci: no cpu_on
method`), which nothing in that userspace tolerates. Confirmed by a control: the
   identical kernel+initrd under a generic `-cpu max` QEMU machine boots init fine
   and fails cleanly on a missing storage partition instead (needs `lpmake` to build
   a `super` image — a real but different, tractable problem, not available here).
2. **This session's own sandbox can't fetch nix-on-droid's other 16 flake inputs.**
   The egress proxy serves only the git protocol for unattached repos; every
   tarball/archive endpoint Nix's `github:` fetcher actually uses is 403 for
   anything except `agentydragon/ducktape`, and `add_repo` can't widen it
   ("cross-tier adds are not supported"). Verified on the host before spending
   guest boot time on it. Switching the inputs to `git+https://` would dodge this,
   but silently stops testing the literal reported command — declined for that
   reason.

So: still no reproduction, but for two well-understood, external reasons (need real
ARM hardware/KVM or a working `super.img`; need a session whose GitHub access
actually covers nix-on-droid's inputs), not because the hypothesis was tested and
failed. Re-attempting the same approach a third time in this environment isn't
expected to get further. See `debug/nix_on_droid_495_pty/` (harness + both attempts'
detailed notes) on branch `debug/nix-on-droid-495-android-vm-repro`.

## Open on-device checks

Everything reachable from inside nix-on-droid's own restricted app context has been
tried (SELinux enforce read, logcat, direct Android binary exec) and hits the same
wall each time. What's left needs either the phone itself doing more than the app's
restricted shell allows, or access outside that context entirely — all cheaper than
continuing the VM route:

- Run `scripts/ptytest.c` (from the VM branch's harness) compiled and executed _as
  the actual nix-on-droid app_ on the phone — replicates Nix's exact
  `posix_openpt`/fork/`open`/`tcgetattr` sequence with per-step `errno`, separating
  which syscall fails from which specific failure, directly on the real device.
- AVC denials via `adb logcat -b all | grep -i avc` from an external computer while
  re-triggering the switch on the phone — sidesteps the in-app exec restrictions
  entirely. Needs a computer to pair/plug the phone into.
- Pulling the phone's actual SELinux policy (`/sys/fs/selinux/policy`, needs real
  root/adb) for a `sesearch` against its real policy rather than an AOSP
  approximation.

## Related

- Upstream: nix-community/nix-on-droid#495 (open, unresolved).
- PRs: #7167 (nixpkgs pin, didn't fix it), #7169 (unrelated git-fetcher bugfix, kept),
  #7170 (revert of #7167).
- VM reproduction branch: `debug/nix-on-droid-495-android-vm-repro`.
