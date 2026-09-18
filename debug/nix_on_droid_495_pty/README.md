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

Two findings are already settled and hold regardless of what the guest does. **The
`ls /dev/pts` denial is not evidence**: `allow domain devpts:dir search` without `read`
means every Android app gets `EACCES` listing that directory, on every release, so it
cannot explain a regression. And **the policy permits the whole sequence Nix runs**,
including `open` on the app's own pty type and the `TCGETS` ioctl, in AOSP `main` as
well as Android 9.

The open question is whether that is what the phone actually does, which only running
the real command answers.
**`nix-on-droid switch --flake github:agentydragon/ducktape?ref=devel#pixel6` has not
been run**, and cannot be in this container, for two independent reasons established
below: the Android 14 arm64 guest does not reach userspace (§ Harness), and this
session's GitHub egress scope refuses the flake's inputs (§ egress scope). Neither is a
statement about the phone.

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

Policy therefore predicts the sequence succeeds, which is what the Android 9 x86_64
guest did. Whether an Android 14 device behaves the same was not established here.

## Harness: Android 14 arm64 (`scripts/arm64/`)

The device is a Pixel 6: **Android 14, aarch64**, kernel
`6.1.157-android14-11-gbd23337e42e7-ab14791245`. The matching test environment is
Google's own `system-images;android-34;google_apis;arm64-v8a` (Android 14, API 34,
`userdebug`, SELinux **enforcing** by default), run under QEMU TCG — there is no
`/dev/kvm` and no `vmx`/`svm` here (the container is itself a Firecracker microVM), and
the guest is aarch64 on an x86_64 host, so acceleration is impossible in both
directions.

`scripts/arm64/` is this harness:

| Script                             | Role                                                         |
| ---------------------------------- | ------------------------------------------------------------ |
| `launch.sh`, `avd-config.ini`      | boot the AVD; always keep `-show-kernel`                     |
| `patch_soundhw.py`, `patch_cpu.py` | inspect/patch the emulator's hardcoded `-soundhw` and `-cpu` |
| `advancedFeatures.diff`            | the feature set that leaves no PCI device behind             |
| `tryboot.sh`                       | bounded boot attempt that prints how far the kernel got      |
| `tryvirt.sh`                       | same kernel+initrd on stock `qemu-system-aarch64 -M virt`    |
| `ghprobe.sh`, `proxytest.sh`       | egress-scope and proxy/CA probes                             |
| `mkca.sh`, `provision.sh`          | build and install the proxy CA into both guest stores        |
| `install_nod.sh`, `run_nod.sh`     | install com.termux.nix; run a command inside its proot       |
| `userenv_build.sh`                 | build a `user-environment.drv` from the reachable channel    |
| `extract_part.py`                  | carve an ext4 partition out of a GPT image (no loop here)    |

Google blocks this combination, and getting past the block took four separate fixes.
Each is a real defect in the linux-x86_64 emulator package, not a policy gate:

1. **The launcher refuses arm64 on an x86_64 host** — `Avd's CPU Architecture 'arm64' is
not supported by the QEMU2 emulator on x86_64 host`. The package nonetheless ships
   `qemu/linux-x86_64/qemu-system-aarch64[-headless]`, which is itself a full launcher
   accepting `-avd`. Calling it directly skips the check. It needs the package's own
   `lib64` on `LD_LIBRARY_PATH` (`libtcmalloc_minimal.so.4`).
2. **It blocks on a Qt crash-consent dialog** with the guest CPU at 0%, which looks
   exactly like a very slow boot. `-crash-report-mode disabled -no-metrics -no-qt` (and
   the `-headless` binary) avoid it.
3. **`-soundhw` is emitted unconditionally**, even with audio disabled in the AVD and
   `-no-audio`/`-audio none` passed, and the only two cards (`hda`, `virtio-snd-pci`)
   are both PCI. QEMU's legacy `soundhw_init()` resolves its bus with
   `pci_find_primary_bus()` and aborts: `PCI bus not available for hda`. There is no
   `none` card, so the launcher's own literal is patched instead —
   `scripts/arm64/patch_soundhw.py` rewrites `-soundhw` to `-D` (QEMU's log-file
   option, which harmlessly swallows the card spec that follows).
4. **arm64 `ranchu` has no PCI bus at all**: with audio gone the next failure is
   `-device virtio-serial-pci: No 'PCI' bus found`. Every PCI device on the generated
   command line comes from an advanced feature, so turning those off
   (`scripts/arm64/advancedFeatures.diff`: `VirtioSndCard`, `VirtioWifi`,
   `VirtioVsockPipe`, `VirtconsoleLogcat`, `VirtioInput`, `BluetoothEmulation`,
   `Mac80211hwsimUserspaceManaged`, `ModemSimulator`) leaves only virtio-mmio devices,
   and QEMU then starts and the kernel boots. Block, net and rng were already
   `virtio-*-device` (mmio).

**It still does not reach userspace.** The kernel — `6.1.23-android14-4`, the Pixel 6's
kernel generation — comes up fine and hands off, and then init dies instantly:

```text
[    1.593294][    T1] Run /init as init process
[    2.702757][    T1] Kernel panic - not syncing: Attempted to kill init! exitcode=0x0000000b
[    2.706293][    T1]  el0_da+0x84/0xe0
```

`exitcode=0xb` with `el0_da` is a SIGSEGV from a user-mode data abort, before init logs
anything. Two more symptoms place the blame on the machine rather than on Android:
`psci: no cpu_on method, not booting CPU1..3` (so only one core ever runs), and the CPU
is `MIDR 0x411fd070` — Cortex-A57, ARMv8.0 — which neither `-qemu -cpu max` nor the
documented `hw.cpu.model` AVD property will change. Google only ships arm64 images for
Apple Silicon hosts, where HVF ignores `-cpu` and the guest sees a real ARMv8.5+ core,
so no upstream configuration exercises this userspace on an emulated ARMv8.0 CPU.

**Gotcha worth keeping**: without `-show-kernel` this failure is invisible. The emulator
passes `-serial null` and `console=0`, adb reports the device as `offline`, and the
QEMU process sits at 100% of one core — indistinguishable from a slow TCG boot. Half an
hour was spent waiting on a guest that had halted at 2.7 seconds. Always boot this
harness with `-show-kernel`.

The same kernel and initrd on **stock `qemu-system-aarch64 -machine virt -cpu max`**
(`scripts/arm64/tryvirt.sh`) confirm the diagnosis: PSCI and PCIe work, the CPU is
honoured, and init runs properly, reaching `FirstStageMain` and failing cleanly on
storage rather than crashing:

```text
init: BlockDevInitializer::InitDevices: partition(s) not found after polling timeout: metadata
init: Failed to mount required partitions early ...
```

That is as far as `virt` can go without more work than it is worth: `fstab.ranchu` marks
`system`/`vendor`/`product`/`system_ext`/`system_dlkm` as `logical`, so first-stage
mount wants a `super` partition with LP metadata plus a by-name `metadata` partition
under `/dev/block/platform/a003c00.virtio_mmio/`. The emulator's ranchu synthesizes
those from the separate `system.img`/`vendor.img` at runtime (its `DynamicPartition`
feature); reproducing it on `virt` needs `lpmake`, which is not available here.

## Guest networking and proxy trust

This container's egress goes through an agent proxy on `127.0.0.1:39587` that
re-terminates TLS, so the guest needs both a route to it and its CA. Neither is
bypassed or stubbed anywhere below.

**Route.** The emulator's user-mode network maps `10.0.2.2` to the host's loopback, so
`10.0.2.2:39587` reaches the proxy directly — no forwarder, no `hostfwd`/`guestfwd`
rule. Verified from the host first, with a client told about the proxy only explicitly
and with no inherited CA environment (`scripts/arm64/proxytest.sh`), because a failure
there would be a host problem rather than a guest one:

```text
cache.nixos.org status=200
nix-on-droid.unboiled.info status=200
```

**Trust, two stores.** Two different stacks make the requests and they do not share a
trust store:

- The `com.termux.nix` APK downloads the bootstrap zip with a Java HTTP client, which
  uses Android's own store and Android's global proxy setting. On Android 14 the live
  store is the conscrypt APEX (`/apex/com.android.conscrypt/cacerts`) rather than
  `/system/etc/security/cacerts`, and the APEX copy has to be a tmpfs overlay
  propagated into zygote's mount namespace or already-running apps keep the old store.
  `scripts/arm64/provision.sh` writes both, hashed as `<subject_hash_old>.0`
  (`683e3c55.0` here), and sets `settings put global http_proxy 10.0.2.2:39587`.
- Nix and curl inside the proot use their **own** bundle, not Android's, so
  `scripts/arm64/run_nod.sh` points `NIX_SSL_CERT_FILE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE`
  at the proxy bundle and sets `http_proxy`/`https_proxy`. `bin/login` execs
  `/usr/bin/env "$@"` when given arguments, so that environment reaches Nix.

### The session's GitHub egress scope blocks the literal command

Getting the route and the trust right is not sufficient, because the proxy also enforces
an **organization egress policy**, and it scopes GitHub by repository. Measured
(`scripts/arm64/ghprobe.sh`):

```text
github.com/nix-community/nix-on-droid/archive/<rev>.tar.gz          403
codeload.github.com/nix-community/nix-on-droid/tar.gz/<rev>         403
api.github.com/repos/nix-community/nix-on-droid/commits/master      403
codeload.github.com/NixOS/nixpkgs/tar.gz/refs/heads/nixos-unstable   403
github.com/nix-community/nix-on-droid/info/refs?service=…           200
codeload.github.com/agentydragon/ducktape/tar.gz/refs/heads/devel    200
api.github.com/repos/agentydragon/ducktape/commits/devel             200
```

Only the **git smart-HTTP protocol** is served anonymously for an unattached public
repo; every tarball and API endpoint is refused. Nix's `github:` fetcher uses the
tarball endpoints, and `agentydragon/ducktape` is the only repo attached to this
session, so `--flake github:agentydragon/ducktape?ref=devel#pixel6` can fetch the
ducktape tree itself but none of its 16 other inputs
(`nix-community/nix-on-droid`, `NixOS/nixpkgs`, `nix-community/home-manager`, …).
`add_repo` cannot widen this: attaching `nix-community/nix-on-droid` is refused with
`cross-tier adds are not supported in v1: session already has repos from owner(s)
[agentydragon]`. nix-on-droid's non-flake path is blocked for the same reason — its
default channel is `github.com/nix-community/nix-on-droid/archive/release-24.05.tar.gz`
(`modules/build/initial-build.nix`), even though `nixos.org/channels/nixos-24.05`
itself answers 200.

This is the failure class `/root/.ccr/README.md` says to report rather than work
around, so it is reported rather than worked around. What _is_ reachable and does work:
`cache.nixos.org`, `channels.nixos.org`/`nixos.org`, and
`nix-on-droid.unboiled.info` (the bootstrap zip).

## Earlier, weaker environment: Android 9 x86_64

The first round used Android-x86 9.0-r2 (Android 9, kernel 4.19.110) under
`qemu-system-x86_64`. It is kept here only because its policy dump is still quoted
above; as a stand-in for a Pixel it is weak on two counts — five Android releases of
SELinux hardening, and the wrong architecture — and its results do not substitute for
the Android 14 arm64 run.

`scripts/` (the x86_64 harness; the arm64 one is `scripts/arm64/`):

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

Guest shell (Android 9 x86_64 only): `ro.debuggable=1` is already in android-x86's `default.prop`, so init's own
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

### Android 9 x86_64 results

These are a probe of the syscall sequence and a synthetic local build, on the wrong
Android version and the wrong architecture. They are evidence about the policy, not
about the phone, and they are **not** a run of `nix-on-droid switch`. All four variants
run the identical sequence from `ptytest.c`.

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
- **SELinux does not deny Nix's pty sequence to an app**, by policy text — Android 9's
  binary policy and AOSP `main` agree — and, on Android 9 x86_64, by execution.
- **nix-on-droid's proot does not break the pty sequence**, at either uid.

What it does **not** rule out, and what the Android 14 arm64 work exists to settle: the
executed half of that is Android 9 on kernel 4.19, while the Pixel 6 is Android 14 on
kernel 6.1. Newer policy and newer devpts/ioctl behaviour are exactly what differs, and
the ordering in the reported error (`open` succeeds, `TCGETS` returns `EACCES`) is not
explained by any rule read so far.

## Next steps, cheapest first

The phone is a far cheaper instrument than the emulator, and it is the only one that can
answer the question directly. Everything here runs on the device in minutes.

1. **Check for an AVC at the moment of failure**: `logcat -b all | grep -i avc` around a
   failing `nix-on-droid switch`. If nothing appears, SELinux is excluded outright and
   this whole line of investigation closes.
2. **Run `scripts/ptytest.c` inside nix-on-droid, as the app.** It reports `errno` per
   step, so it separates "which syscall" from "which failure". If `tcgetattr` fails
   there while every earlier step passes, that pins the failure precisely.
3. **Query the phone's own policy** rather than an AOSP approximation: pull
   `/sys/fs/selinux/policy` and run `sesearch --allow -t untrusted_app_all_devpts` and
   `--allowxperm`. If `0x5401` (`TCGETS`) is present and `open` is allowed, SELinux
   cannot be producing this error.
4. If SELinux is excluded, follow the nixpkgs 2026-01-24 → 2026-01-31 window instead.
   Downgrading Nix alone reportedly does not help, which points below Nix; glibc's pty
   helpers are the obvious candidate given the `open`-succeeds-then-`TCGETS`-fails
   shape.

Resuming the emulator work needs a host with `/dev/kvm` and an arm64 CPU (where the
emulator is supported and fast), or `lpmake` to build a `super` image for the stock-QEMU
`virt` route. It also needs a session whose GitHub egress covers the flake's inputs.

## Not done

- **The literal command was never run.** Two independent blockers, either sufficient on
  its own: the Android 14 arm64 guest panics in init (§ Harness), and its flake inputs
  are GitHub tarballs this session's egress policy refuses (§ egress scope). Both are
  environment limits; neither says anything about the phone.
- **The guest-side proxy and CA work was never exercised end to end.** The route and the
  bundle are verified from the host only; `scripts/arm64/provision.sh` and the
  `curl https://cache.nixos.org` check from inside the proot need a booted guest.
- On Android 9 x86_64, `pm install` of the APK repeatedly killed `system_server`
  (`Failure calling service package: Broken pipe`) under TCG load, so the bootstrap was
  unpacked by hand instead — the same steps the app performs (unzip, replay
  `SYMLINKS.txt` and `EXECUTABLES.txt`, since an Android-written zip carries neither
  symlinks nor the exec bit).
- The bootstrap used is `bootstrap-release-24.05` (Nix 2.20.5), the app's default, not
  one built from the pinned `df611d53…`. It still carries the `nix/var/var` bug that
  pinned commit fixes (`db.sqlite` lands at `nix/var/var/nix`, so Nix cannot create
  `/nix/var/nix/temproots`); `install-nod.sh` does not repair it, so a resumed run must
  merge those directories and `chmod u+w` the 0555 state dirs by hand.
- Boot time is the binding practical cost: Android 14 arm64 under TCG needs tens of
  minutes to reach `sys.boot_completed`, with one host core saturated throughout.
