# nix-on-droid #495: "getting pseudoterminal attributes: Permission denied"

Active investigation into <https://github.com/nix-community/nix-on-droid/issues/495>,
which `nix-on-droid switch` on the Pixel 6 (<nix/droid/hosts/pixel6.nix>) hits while
building `user-environment.drv`:

```text
error: getting pseudoterminal attributes: Permission denied
error: … while waiting for the build environment for
       '/nix/store/….-user-environment.drv' to initialize (failed with exit code 1)
```

The working hypothesis under test was **SELinux**: on the phone a bare `ls -la /dev/pts`
also returns `Permission denied` although the DAC bits (`/dev/pts` 0755, `/dev/ptmx` 0666) allow it, which is the classic fingerprint of a MAC layer. Testing it needs a real
Android kernel with Android's policy loaded, which a generic Linux container cannot
provide.

**Result: the hypothesis is not supported.** Android's SELinux policy explicitly permits
every operation Nix performs on a pty, and the whole sequence — up to and including a
real local Nix build inside nix-on-droid's own proot, as an app uid, in the
`untrusted_app` domain — ran clean on a real Android kernel with zero devpts AVC
denials. The `ls /dev/pts` denial is normal Android behaviour, not a clue. See
[What this rules out](#what-this-rules-out) for the caveat that keeps this from being
conclusive for the phone itself.

## Where the error comes from

Nix 2.31.3, `src/libstore/unix/build/derivation-builder.cc`. `startBuilder()` opens the
master; `openSlave()` runs **in the forked child**:

```cpp
    AutoCloseFD builderOut = open(slaveName.c_str(), O_RDWR | O_NOCTTY);
    if (!builderOut) throw SysError("opening pseudoterminal slave");
    struct termios term;
    if (tcgetattr(builderOut.get(), &term))
        throw SysError("getting pseudoterminal attributes");   // <-- #495
```

So on the phone the `open()` of the slave **succeeds** and the following
`ioctl(fd, TCGETS)` returns `EACCES`. That ordering is the sharpest constraint available:
any explanation has to deny `TCGETS` while allowing `open`.

Two things this fixes about the problem statement:

- `grantpt()` is inside `#ifdef __APPLE__` in 2.31.3, so it is not involved on Android.
- The `chmod`/`chown` on the slave only happen on the `buildUser` branch, which
  nix-on-droid (no `build-users-group`) never takes.

This code runs once per **local** build and not at all for a path fetched from a binary
cache, so a reproduction needs a genuine local build rather than a cache hit.

## What Android's policy actually says

From the binary policy shipped in the android-x86 ramdisk (`sesearch`), cross-checked
against AOSP `main` (`private/app.te`, `private/domain.te`, `public/te_macros`), so it
describes a Pixel too:

```text
allow domain ptmx_device:chr_file rw_file_perms;
allow domain devpts:dir search;                        # search, but NOT read
allow appdomain devpts:chr_file { getattr read write ioctl };
type_transition untrusted_app devpts:chr_file untrusted_app_all_devpts;
allow untrusted_app_all untrusted_app_all_devpts:chr_file { getattr ioctl open read write };
allowxperm untrusted_app_all untrusted_app_all_devpts:chr_file ioctl
    { 0x5401-0x5403 0x540b 0x540e-0x5411 0x5413-0x5414 0x5451 };
```

- **`ls /dev/pts` failing is a red herring.** `allow domain devpts:dir search` without
  `read` means _every_ Android app gets `EACCES` listing that directory, on every
  release. It has always been true, so it cannot explain a regression, and it says
  nothing about whether a pty can be used.
- The `create_pty()` macro gives each app domain its **own** devpts type via
  `type_transition`, and that type does grant `open`. Verified live: a pty created by an
  `untrusted_app` process is labelled `u:object_r:untrusted_app_all_devpts:s0:c512,c768`.
- The ioctl allowlist (`unpriv_tty_ioctls`) contains `0x5401` = `TCGETS` and `0x5402` =
  `TCSETS`. **`tcgetattr` is explicitly permitted.**
- The one pty permission apps lack is `setattr` (`chmod`/`chown`) — Nix's `buildUser`
  branch, not this one.

Policy therefore predicts the sequence succeeds, which is what the guest did.

## Harness

No `/dev/kvm` and no `vmx`/`svm` in `/proc/cpuinfo`: this container is itself a
Firecracker microVM (`6.18.44-fc-v33`), so nested virtualisation is unavailable and
everything below is QEMU **TCG** software emulation. Android-x86 9.0-r2 x86_64 was
picked over the Google emulator images because the official `emulator` binary wants KVM,
and over aarch64 images because x86_64 avoids emulating a second architecture.

Guest: Android 9, kernel 4.19.110, `qemu-system-x86_64 -machine pc,accel=tcg -cpu max
-smp 4 -m 6144`. Boot to `sys.boot_completed=1` takes ~12 minutes.

`scripts/` is the whole harness, in dependency order:

| Script                                   | Role                                                      |
| ---------------------------------------- | --------------------------------------------------------- |
| `mkinitrd.sh`, `initrd-5-custom`         | repack the ISO's initrd with an injected boot hook        |
| `boot.sh`, `run.sh`                      | QEMU invocation and detached start                        |
| `serial.py`                              | scripted serial console: `relay`, `run`, `push`           |
| `mkpayload.sh`                           | load the guest's payload disk                             |
| `ptytest.c`                              | the Nix pty sequence, step by step, with per-step `errno` |
| `install-nod.sh`                         | install the nix-on-droid bootstrap by hand                |
| `prootrun.sh`, `asapp.sh`, `nixbuild.sh` | run a payload inside the proot as the app                 |

Three host-side constraints worth knowing before resuming:

- The host kernel has no `iso9660`, `ext4` or loop support, so the ISO is unpacked with
  `bsdtar` and the guest's ext4 images are populated with `debugfs` rather than mounted.
- Booting `-kernel`/`-initrd` rather than the ISO's bootloader is what allows setting
  `androidboot.selinux` and a serial console at all.
- android-x86's `/init` silences the console (`echo 0 0 0 0 > printk`) unless `DEBUG` is
  set, and `DEBUG` also stops for an interactive shell and downgrades `switch_root` to
  `chroot`. `initrd-5-custom` restores printk instead; without it a failing boot is
  completely silent.

Guest shell: `ro.debuggable=1` is already in android-x86's `default.prop`, so init's own
`console` service (`seclabel u:r:shell:s0`) starts a shell on `/dev/console`, which
`console=ttyS0,115200` puts on the serial port. No adb needed.

## SELinux enforcing is not reachable on android-x86

`androidboot.selinux=enforcing` loads the policy and flips to `enforcing=1`, then dies
immediately:

```text
selinux: SELinux: Loaded policy from /sepolicy
avc: denied { search } for pid=1 comm="init" name="/" dev="tmpfs" ino=7180
     scontext=u:r:kernel:s0 tcontext=u:object_r:tmpfs:s0 tclass=dir permissive=0
init: Unable to write to /sys/fs/selinux/checkreqprot: open() failed: Permission denied
init: Reboot start, reason: reboot, rebootTarget: bootloader
init[1]: segfault at 14 ip 000000000056d30b sp 00007ffcd24ad100 error 4 in init
```

android-x86 `switch_root`s into a **tmpfs**, while AOSP policy expects the root
filesystem to be labelled `rootfs`; `u:r:kernel:s0` has no `search` on `tmpfs`. That is
structural to android-x86's boot design and is why the project ships permissive.
`rootcontext=`/`context=` mount options do not fix it: `context=` pins every inode to one
type, which then denies `/init` its `init_exec` label and breaks the
`kernel`→`init` domain transition.

The runs below are therefore **permissive**. That still answers the question, because
permissive logs every denial it would have enforced (`permissive=1`) — so an absent AVC
means the access was genuinely allowed, not merely un-blocked.

## What was run, and what happened

All four variants run the identical sequence from `ptytest.c`.

| Variant                                                      | Result          |
| ------------------------------------------------------------ | --------------- |
| uid 0, `u:r:shell:s0`                                        | PTY SEQUENCE OK |
| uid 0, `runcon u:r:untrusted_app:s0:c512,c768`               | PTY SEQUENCE OK |
| uid 0, `untrusted_app`, **inside nix-on-droid's proot**      | PTY SEQUENCE OK |
| uid 10199 (`u0_a199`), `untrusted_app`, **inside the proot** | PTY SEQUENCE OK |

The last one is the faithful case — the app's own uid, the app's own SELinux domain, the
bootstrap unpacked into `/data/data/com.termux.nix/files/usr` owned by `u0_a199` and
labelled `u:object_r:app_data_file:s0:c512,c768`, entered through the exact proot binds
and flags that `modules/environment/login/login.nix` generates (`--link2symlink
--sysvipc`, `-b /:/android`, no `-r`, no fakeroot):

```text
== ptytest: uid=10199 euid=10199 gid=10199
  posix_openpt(O_RDWR|O_NOCTTY)      ok
  slave path = /dev/pts/1
  slave stat  = mode=0600 uid=10199 gid=10199
  chmod(slave,0600) [buildUser path] ok
  chown(slave,uid,0) [buildUser path] FAIL errno=1 (Operation not permitted)
  grantpt(master)                    ok
  unlockpt(master)                   ok
-- child pid=13276 (openSlave)
  child open(slave,O_RDWR|O_NOCTTY)  ok
  child tcgetattr  <-- issue #495    ok
  child tcsetattr(TCSANOW)           ok
== RESULT: PTY SEQUENCE OK
```

The only failure is `chown` with `EPERM` — ordinary DAC (a non-root user cannot chown to
gid 0), not `EACCES`, and on the `buildUser` branch nix-on-droid never takes.

Then a genuine **local** Nix build inside the proot as the app, substituters emptied so
it cannot be satisfied from a cache and must run the pty code:

```text
== nix version ==
nix (Nix) 2.20.5
== building a trivial derivation locally ==
/nix/store/ng812x7cfplll7ga45969l3jg0sn9nvi-pty-probe
== build rc=0 ==
```

Across every run, `dmesg | grep avc` shows **no denial on `devpts`, `ptmx_device` or any
pty type**. The only denials the nix/proot processes produced were on `/proc/stat`:

```text
avc: denied { open } for pid=13875 comm="nix" path="/proc/stat"
     scontext=u:r:untrusted_app:s0:c512,c768 tcontext=u:object_r:proc_stat:s0
```

which is a good sanity check on the harness — that denial is exactly why `login.nix`
carries its `fakeProcStat` bind.

## What this rules out

- **The `ls /dev/pts` denial is not evidence.** It is `allow domain devpts:dir search`
  without `read`, true for every app on every Android release.
- **SELinux does not deny Nix's pty sequence to an app**, by policy text (Android 9 and
  AOSP `main`) and by execution.
- **nix-on-droid's proot does not break the pty sequence**, at either uid.
- A local Nix build inside that proot is not inherently broken on Android.

The caveat that keeps this from closing the issue: the guest is **Android 9 on kernel
4.19**, and the Pixel 6 is Android 15/16 on kernel 5.10/6.1. The most likely places for a
real difference — newer policy, newer devpts/ioctl behaviour — are exactly what differs.
This is a strong negative, not a proof.

## Next steps, cheapest first

1. **Run `ptytest.c` on the phone**, inside nix-on-droid, as the app. It reports `errno`
   per step and separates "which syscall" from "which failure". If `tcgetattr` fails
   there and every earlier step passes, that pins the phone-side failure precisely.
2. **Check for an AVC at the moment of failure** on the phone: `logcat -b all | grep -i
avc` around a failing `nix-on-droid switch`. If nothing appears, SELinux is excluded
   outright on the device itself and this investigation can stop.
3. **Query the phone's own policy**: pull `/sys/fs/selinux/policy` and run
   `sesearch --allowxperm -t untrusted_app_all_devpts` plus `--allow`. That is the
   authoritative answer for that device rather than an AOSP approximation.
4. If SELinux is excluded, follow the nixpkgs 2026-01-24 → 2026-01-31 window instead. The
   reporter found downgrading Nix alone does not help, which points at something under
   Nix rather than Nix — glibc's pty helpers are the obvious candidate given the
   `open`-succeeds-then-`TCGETS`-fails shape.

## Not done

- **The real APK was never installed.** `pm install` of `com.termux.nix` 188037 (it does
  ship `lib/x86_64`) repeatedly killed `system_server` with `Failure calling service
package: Broken pipe` — load average was above 20 on a 4-core TCG host. The bootstrap
  was installed by hand instead, which is what the app itself does: unzip
  `bootstrap-x86_64.zip`, replay `SYMLINKS.txt` and `EXECUTABLES.txt` (an Android-written
  zip carries neither symlinks nor the exec bit), then run `bin/login`.
- **`nix-on-droid switch` against `pixel6.nix` was never run.** It needs network from
  inside the guest, and the guest's route out is QEMU usermode NAT, which is not this
  container's HTTPS proxy; `cache.nixos.org` is unreachable from the guest and the
  proxy's CA is not in Android's trust store. Not worked around — the bootstrap zip was
  staged from the host over a virtual disk instead, which was enough to get a local build.
- The bootstrap used is `bootstrap-release-24.05` (Nix 2.20.5), the app's default, not
  one built from the pinned `df611d53…`. It still carries the `nix/var/var` bug that
  pinned commit fixes (`db.sqlite` lands at `nix/var/var/nix`, so Nix cannot create
  `/nix/var/nix/temproots`); `install-nod.sh` does not repair it, so a resumed run must
  merge those directories and `chmod u+w` the 0555 state dirs by hand.
