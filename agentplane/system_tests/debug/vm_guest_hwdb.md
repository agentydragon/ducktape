# Prebuilt VM hardware database

The Debian test guest extracts packages without running their installation scripts.
Its udev package supplies 33 hardware database sources (9.1 MB), but no `hwdb.bin`.
Each fresh root overlay therefore compiles the database during boot.
`systemd-udevd.service` orders itself after `systemd-hwdb-update.service`, so this
delays discovery of the disks needed by Runner.

[PR #13011](https://github.com/agentydragon/ducktape/pull/13011) builds the database
once with the guest closure's `systemd-hwdb`, loader, and libraries, and packages
it at `/usr/lib/udev/hwdb.bin`. With no `/etc/udev` overrides, the upstream unit's
trigger conditions skip rebuilding it. Device discovery still runs normally.

## Paired measurements

Both full `//agentplane/system_tests/suites:test_vm` runs passed on RBE on
2026-10-30, using base `02f8658166b9cd6d6d4ab17e9157dbb52d959761`.
The candidate contains the guest patch committed as
`aaae78285225ec03ba248e99cbded27d0c66b898`.

| Measurement, seconds          | Baseline | Prebuilt |
| ----------------------------- | -------: | -------: |
| Test action                   |    464.4 |    438.9 |
| Stack ready                   |    102.4 |    120.5 |
| Credentials and sampler group |    139.6 |    134.1 |
| Three-VM turn/OOM group       |    187.6 |    149.2 |
| Complete Bazel invocation     |    501.1 |    607.9 |

The test action shortened by **25.6 s (5.5%) in this pair**. The two scenario
groups together shortened by 43.9 s while stack startup grew by 18.1 s. These
are individual samples, not a stable whole-suite speedup. BuildBuddy reported the
same executor ID for both actions, and they overlapped for 262 s; shared load and
startup variation remain confounders.

| Guest              | Wait for Ready, baseline → prebuilt | QEMU start → Ready, baseline → prebuilt |
| ------------------ | ----------------------------------: | --------------------------------------: |
| Claude credentials |                         78.8 → 72.6 |                             54.1 → 44.7 |
| Codex credentials  |                         78.8 → 70.6 |                             54.1 → 45.9 |
| Claude turn        |                         99.6 → 74.5 |                             73.6 → 49.9 |
| Codex turn         |                         80.9 → 58.5 |                             54.9 → 35.7 |
| Codex tool OOM     |                         83.0 → 60.6 |                             53.6 → 36.1 |

The baseline's five traced guests spent 13.0–15.3 s rebuilding the database.
Earlier runs showed 19.5–23.7 s, illustrating worker variation. In the candidate,
**all six guest consoles explicitly report that the rebuild unit was skipped**.
Both runs retain six independent launcher console identities and six successful
shutdown receipts. The sampler has no common readiness span, so it is omitted
from the per-guest table. Claude's interval includes its private supervisor checks.

The complete candidate invocation took longer because it built a new guest image
while the baseline reused its build artifacts. The new database action itself
took 10.0 s and is cached by its closure and tool inputs. A native closure probe
produced a 13,201,901-byte database with identical bytes across two builds and a
successful Virtio PCI lookup. The database adds about 13.2 MB to the root image.
The cold invocation comparison does not establish an end-to-end CI speedup.

## Evidence and reproduction

- [Baseline invocation](https://app.buildbuddy.io/invocation/8d62891e-1178-4d76-9c62-ab2f729bc0b4).
- [Prebuilt invocation](https://app.buildbuddy.io/invocation/1e21b661-87ab-4e31-9361-e7a32aba0496).

Run `bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results
--test_output=errors`. The candidate also explicitly built
`//agentplane/system_tests/vm/guest:hwdb` for its Python lint aspects.

Use the target's Bazel `TestResult.testAttemptDuration` for test-action time and
`k3s-timeline.jsonl` for stack readiness. `vm-phases.jsonl` supplies enclosing
`vm.case` durations and each `vm.sandbox.ready` interval; follow span parents to
the named scenario. Match Sandbox names to launcher and console logs in
`vm-artifacts/`. QEMU start comes from the launcher's `Domain started.` event;
unit start/finish and skip messages come from CRI-timestamped guest console logs.
Initial console output can be buffered, so its first kernel line is not an
exact kernel-start timestamp. Overlapping service durations are not additive.

## Staging and further trimming

The staging NixOS image already prebuilds this database. At pinned nixpkgs
`6fcc160353f63555b560f25588925133c8f61f2a`, the
[udev module](https://github.com/NixOS/nixpkgs/blob/6fcc160353f63555b560f25588925133c8f61f2a/nixos/modules/services/hardware/udev.nix#L170)
builds `hwdb.bin` and installs it under `/etc/udev`; the
[systemd module](https://github.com/NixOS/nixpkgs/blob/6fcc160353f63555b560f25588925133c8f61f2a/nixos/modules/system/boot/systemd.nix#L72)
omits the rebuild unit. No staging image change is needed for this mechanism.
This checks the pinned image definition, not a running staging guest.

The Debian closure also carries about 35.5 MiB of locales, documentation, and
man pages. Removing those could reduce image transfer size; its boot-time effect
has not been measured. The guest also attempts `ldconfig.service` without an
installed `ldconfig` executable. These are separate candidates from the measured
hardware database change.
