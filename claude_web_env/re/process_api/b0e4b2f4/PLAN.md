# Remaining Verification Work

See <../README.md> for completed work (binary analysis, decompilation,
translation, build).

## Completed (2026-02-13)

String differential analysis identified ~60 gaps across all modules (format
string mismatches, missing logs, missing code paths). All remediated in
phases A–D:

- **Phase A** — Fixed ~20 format strings (colons, casing, wording)
- **Phase B** — Added ~10 missing debug log messages
- **Phase C** — Added ~30 missing code paths:
  - C1: `io.rs` — extracted `process_ws_message`/`forward_stdin`, added ExpectStdIn protocol, explicit WS close, exit_status_rx in select! loop
  - C2: `cgroup.rs` — v2 nested detection via /proc/self/cgroup, mkdir -p fallback, cpu,cpuacct v1, controller enable helper
  - C3: `control_server.rs` — local IP rejection, healthcheck /proc readings, shutdown complete log
  - C4: `oom_killer.rs` — orphan adoption before scan, post-kill timing, failed notify log, error handling
  - C5: `main.rs` — format fixes, SIGINT message, graceful shutdown messages, blocked IPs log, WS buffer size log
  - C6: `state.rs`/`adopter.rs`/`proc_handle.rs` — lowercase "process not found", OOM kill logs, `try_adopt_orphans`
- **Phase D** — Re-ran string diff: all application-level strings present (library noise from dep version diffs accepted)

Dependency version mismatches (tungstenite 0.24→0.28, nix 0.29→0.31,
clap_lex 0.7→1.0, rustc 1.83 vs differs) are accepted — no behavioral impact.

## Completed (2026-02-13, session 2)

- **C7: Structural type enrichment**
  - Added `ProcessInfo`, `CgroupConfig`, `ProcController` serde types matching
    binary field names (from disassembly at 0x21c970, 0x21ce40, 0x21c870)
  - Renamed fields: `cgroup_path`→`memory_cgroup_path`, `state`→`internal_state`,
    `handle`→`proc_handle`
  - Added `WsStreamHandle` struct with `process_info`/`controller` fields
  - Added all channel endpoints to `ProcHandle` (`stop_waiting_tx/rx`,
    `exit_status_tx/rx`, `oom_killed_tx/rx`)
  - Fixed healthcheck: `GET /health` → `"OK\n"`, added `GET /container_name`
  - Added `GET /healthcheck` diagnostic endpoint (reads `/proc/self/limits`,
    `/proc/sys/kernel/pid_max`, runs `ps aux --no-headers`)
  - Manual `Clone` impl for `ProcController` (oneshot::Sender not Clone)
  - Populated `ws_tx.process_info`/`ws_tx.controller` in `handle_create_process`
  - Serde field strings now present in RE binary: `internal_state`,
    `memory_usage_bytes`, `memory_cgroup_path`, `process_group_pid`,
    `process_info`, `start_time`

## Open Items

### Behavioral testing (Phase E)

Write a WebSocket test harness exercising the protocol against both binaries:

1. `CreateProcess` → stdout + `ProcessExited`
2. Wrong `expected_container_name` → rejection
3. `ProcessConnection` reattach to detached process
4. `SendSignal` to running process → `SignalSent`
5. Memory hog → `ProcessOutOfMemory` / `ContainerOutOfMemory`
6. `StdInEOF` → stdin closes
7. Local IP with `--block-local-connections` → rejected

## Status

- [x] Binary analysis, decompilation, translation, build
- [x] String differential analysis + remediation (Phases A–D)
- [x] String coverage diff passes (application-level strings)
- [x] Every function has `Decompiled from 0x...` annotation
- [x] C7: Structural type enrichment
- [ ] Behavioral test harness written
- [ ] Behavioral tests pass against both binaries
