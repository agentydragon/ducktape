# process_api RE Plan

## Current Binary

| Property        | Value                                         |
| --------------- | --------------------------------------------- |
| **Build ID**    | `edebff2c28de76238c95c299ba3401a9098c9e17`    |
| **Release**     | `process_api_2026-05-11-18-55`                |
| **Size**        | 4,377,896 bytes                               |
| **Linking**     | Static-pie                                    |
| **Rust**        | `rustc 1.95.0-nightly (6a979b3e3 2026-02-26)` |
| **Source path** | Remapped — modules appear as bare `src/*.rs`  |

## RE Status by Module

`edebff2c verified` means the module's strings and structural evidence were
checked against this binary. `offsets` says whether the `Decompiled from 0x...`
annotations in the source point at this binary.

| Module                 | Status            | Offsets          | Notes                                                               |
| ---------------------- | ----------------- | ---------------- | ------------------------------------------------------------------- |
| `main.rs`              | edebff2c verified | partial          | CLI blob re-anchored; shutdown grace added                          |
| `io.rs`                | edebff2c verified | partial          | `cpu_timeout`, `accept_zstd`, `supports_zstd`, `ProcessCpuTimedOut` |
| `proc_handle.rs`       | edebff2c verified | partial          | `read_cpu_usage_usec`, `ExitReason::CpuTimedOut`                    |
| `control_server.rs`    | edebff2c verified | stale            | `EtcFiles.ca_cert`; `/container_info.json` path change              |
| `firecracker_init.rs`  | edebff2c partial  | new section only | CA fan-out recovered structurally, bodies stubbed                   |
| `ws_compression.rs`    | edebff2c partial  | edebff2c         | New module; zstd params exact, stream pumps stubbed                 |
| `cgroup.rs`            | strings verified  | stale (b0e4b2f4) | No application-string delta                                         |
| `oom_killer.rs`        | strings verified  | stale (91c789ff) | No application-string delta                                         |
| `adopter.rs`           | strings verified  | stale (b0e4b2f4) | No application-string delta                                         |
| `state.rs`             | strings verified  | stale (b0e4b2f4) | No application-string delta                                         |
| `pid_tree.rs`          | strings verified  | n/a              | No application-string delta                                         |
| `platform/unix/mod.rs` | strings verified  | stale            | No application-string delta                                         |
| `trace.rs`             | **not recovered** | —                | Exists in the binary; no source file yet                            |

## Completed

- [x] `ws_compression.rs` — new module; zstd level 3 / windowLog 15 /
      d_windowLogMax 15 / 32 KiB buffers / 64 MiB decompression cap, and the six
      panic line numbers (69, 71, 87, 104, 106, 120)
- [x] `CreateProcess.cpu_timeout` (serde element count 10 -> 11)
- [x] `ProcessConnection.accept_zstd` (serde element count 4 -> 5)
- [x] `ConnectionCapabilities.supports_zstd`
- [x] `ServerMessage::ProcessCpuTimedOut { cpu_timeout_secs, details }`
- [x] `ProcessInfo.cpu_timeout` (FIELDS array at `.data.rel.ro` 0x422c30, 14 entries)
- [x] `EtcFiles.ca_cert` (serde element count 2 -> 3)
- [x] CPU-time enforcement from `<cgroup>/cpu.stat` `usage_usec`
- [x] Egress-CA fan-out in `firecracker_init.rs` — path tables, argv, log
      templates and the twelve helper function boundaries
- [x] Shutdown grace period + `[WARN] N task(s) still alive ...`
- [x] `/container_info.json` (was `../container_info.json`)
- [x] `fuse_spawn ok` status fragment removed from the init status line
- [x] Dependency versions re-read from the binary's panic paths
- [x] Recovered source builds clean (rustc + clippy via `bbr build`)

## Open Items

- [ ] `trace.rs` — recover the module. Evidence available: source-path panic
      locations at `.data.rel.ro` 0x4211f0 / 0x421208 / 0x421220, the
      `##TRACE##` marker at 0x4211e0, the `TraceEventMsg` fields
      (`process`, `host`, `sph`, `cat`, `dur_us`) and the `trace_emitted` /
      `trace_outcome` `ProcessInfo` fields. The `cpu_timeout` trace-outcome
      label is at 0x39a886.
- [ ] `ws_compression.rs` — decompile the `ZSTD_compressStream2` /
      `ZSTD_decompressStream` pumps (0x1bd5c0..0x1bf1bf and the decode arm
      inlined at 0x14c460..0x14dbb0), and identify the GOT slots 0x42bf58 /
      0x42bfa0 (both called as `(ctx, 1, 0)`).
- [ ] `firecracker_init.rs` CA fan-out — decompile the helper bodies; in
      particular the Chromium managed-policy JSON schema, which has no
      distinguishing key string in `.rodata`.
- [ ] `main.rs` — identify where the surviving-task count read at
      `0x98(%r13)` (fn 0x154810) comes from.
- [ ] Re-anchor the remaining stale offsets (`cgroup.rs`, `oom_killer.rs`,
      `adopter.rs`, `state.rs`, `platform/unix/mod.rs`, most of
      `control_server.rs`).
- [ ] Behavioral test harness.

## Method Notes

- rustc 1.95 packs `format_args!` into a byte template: length-prefixed literal
  chunks, `0xc0` placeholder markers, `0x00` terminator. A no-argument
  formatted call passes `(len << 1) | 1` in the argument slot instead of a
  pointer — that is why `panic!("literal")` sites show odd immediates such as
  `$0x35` for a 26-character message.
- Serde `FIELDS` arrays live in `.data.rel.ro` as `(ptr, len)` pairs; the
  pointers arrive as `R_X86_64_RELATIVE` relocations, so `readelf -r` plus the
  adjacent length word recovers field names in declaration order. This is the
  most reliable way to diff wire structs between builds.
- Function boundaries on this stripped binary come from the set of `call`
  targets in `objdump -d` output; that is exact enough for the small helpers
  and approximate for async state machines, which are one giant function each.
