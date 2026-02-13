# Remaining Verification Work

See <../README.md> for completed work (binary analysis, decompilation,
translation, build).

## String Differential Analysis (2026-02-13)

Ran `strings -n 6` on both binaries, filtered to application-level strings,
and diffed. The RE compiles and runs, but the string diff reveals **many gaps**
across all modules. The gaps fall into these categories:

### Dependency Version Mismatches (accepted)

| Crate        | Reference | RE      |
| ------------ | --------- | ------- |
| tungstenite  | 0.24.0    | 0.28.0  |
| nix          | 0.29.0    | 0.31.1  |
| clap_builder | 4.5.56    | 4.5.58  |
| clap_lex     | 0.7.7     | 1.0.0   |
| rustc        | 1.83.0    | differs |

These cause stdlib/library string noise in the diff but do not affect
application logic. Accepted as-is.

### Section Sizes

| Section   | Reference | RE (debug) |
| --------- | --------- | ---------- |
| `.text`   | ~1.6 MB   | ~6.7 MB    |
| `.rodata` | ~120 KB   | ~610 KB    |

RE is unstripped debug build so the size difference is expected. A release
build comparison would be more meaningful but is not a blocker.

## Gap Inventory

Below is every **application-level string** present in the reference binary
but missing or wrong in the reconstruction. Each gap is tagged with the
source file it belongs to and a severity:

- **S** (structural): Missing code path or function. Requires new logic.
- **F** (format): String exists but has wrong wording. Fix the format string.
- **M** (minor): Missing debug log. One-line fix.

### 1. `io.rs` — WebSocket message processing

| #    | Severity | Reference string                                                                          | Gap                                                               |
| ---- | -------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| 1.1  | S        | `"Failed to receive message: "`                                                           | Missing error path in WS read loop                                |
| 1.2  | S        | `"[DEBUG] Failed to send response: "`                                                     | Missing error handling on `send_msg` calls                        |
| 1.3  | M        | `"[DEBUG] process_ws_message returned: "`                                                 | Missing debug log after WS loop returns                           |
| 1.4  | M        | `"[DEBUG] process_ws_message failed: "`                                                   | Missing debug log on WS loop error                                |
| 1.5  | S        | `"[DEBUG] Error starting process: "`                                                      | Missing error path in process spawn                               |
| 1.6  | S        | `"Failed to convert stdin to ChildStdin: "`                                               | Missing stdin handle conversion error                             |
| 1.7  | S        | `"Closing websocket"` / `"error closing websocket: "`                                     | Missing explicit WS close on cleanup                              |
| 1.8  | M        | `"[DEBUG] Process stream is closed"`                                                      | Missing stream-closed detection                                   |
| 1.9  | S        | `"Expected binary message after ExpectStdIn"`                                             | Missing protocol enforcement: binary must follow ExpectStdIn      |
| 1.10 | S        | `"No message received after ExpectStdIn"` / `"Error receiving message after ExpectStdIn"` | Missing ExpectStdIn follow-up read                                |
| 1.11 | S        | `"Expected text message as first control process message"`                                | Missing/wrong first-message validation                            |
| 1.12 | S        | `"process_ws_message: Shutting down, terminating"`                                        | Different shutdown message in WS loop                             |
| 1.13 | S        | `"process_ws_message: Timeout"`                                                           | Missing timeout notification in WS loop                           |
| 1.14 | S        | `"process_ws_message: OOM"` / `"process_ws_message: Container OOM"`                       | Missing OOM notification in WS loop                               |
| 1.15 | F        | `"signal: , core_dumped: , stopped_signal: , continued: "`                                | Exit format includes stopped_signal and continued fields          |
| 1.16 | S        | `"[DEBUG] forward_stdin: Starting stdin forwarding for process"`                          | `forward_stdin` appears to be a **separate function**, not inline |
| 1.17 | M        | `"[DEBUG] Stopping waiting for process"`                                                  | Missing debug log                                                 |
| 1.18 | S        | `"[DEBUG] Failed to send stop signal for process ), error: "`                             | Missing stop signal error handling                                |
| 1.19 | M        | `"WebSocket closed, remaining() = "`                                                      | Missing remaining-bytes log on WS close                           |
| 1.20 | S        | `"failed to match bind"` / `"failed to read message"`                                     | Missing match/read error paths                                    |

**Structural observation:** The WS message loop (`process_ws_message`) appears
to be a **separate named function** (not inlined in `handle_create_process`)
that returns a result which is logged. The `forward_stdin` is also a separate
async function spawned as a task. The current RE inlines both.

### 2. `main.rs` — Entry point and startup

| #   | Severity | Reference string                                             | Gap                                                   |
| --- | -------- | ------------------------------------------------------------ | ----------------------------------------------------- |
| 2.1 | S        | `"[SECURITY] Blocking connections from local IPs: "`         | Missing startup log listing blocked IPs               |
| 2.2 | F        | `"[DEBUG] Received SIGINT, initiating shutdown"`             | RE has `"Caught signal SIGINT!"` — wrong wording      |
| 2.3 | S        | `"All connections and monitors closed..."`                   | Missing graceful shutdown message                     |
| 2.4 | S        | `"Performing graceful shutdown..."`                          | Missing graceful shutdown message                     |
| 2.5 | M        | `"with web socket buffer size of "`                          | Missing WS buffer size startup log                    |
| 2.6 | M        | `"[DEBUG] got shutdown channel rx"`                          | Missing debug log                                     |
| 2.7 | F        | `"[INFO] process_api release: process_api_2026-02-02-04-57"` | RE has `"release "` not `"release: "` (missing colon) |
| 2.8 | F        | `"[INFO] process_api package version: 0.1.0"`                | RE has `"version "` not `"version: "` (missing colon) |

### 3. `control_server.rs` — HTTP control server

| #   | Severity | Reference string                                            | Gap                                                   |
| --- | -------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| 3.1 | M        | `"[CONTROL] Control server shutdown complete"`              | Missing shutdown-complete log                         |
| 3.2 | S        | `"[CONTROL] [SECURITY] Rejected connection from local IP "` | Control server also rejects local IPs                 |
| 3.3 | S        | `"Process limit (soft/hard): "`                             | Healthcheck missing: reads `/proc/self/limits`        |
| 3.4 | S        | `"Total system processes: "`                                | Healthcheck missing: reads `/proc` PID count          |
| 3.5 | S        | `"System PID max: "`                                        | Healthcheck missing: reads `/proc/sys/kernel/pid_max` |

### 4. `cgroup.rs` — cgroup setup and management

| #    | Severity | Reference string                                                                                       | Gap                                                                              |
| ---- | -------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| 4.1  | F        | `"[DEBUG] root: subtree_control: "` / `"[DEBUG] root: current_controllers: "`                          | Format uses `root:` prefix with colon, RE uses different format                  |
| 4.2  | S        | `"[DEBUG] process_api: current_controllers: "`                                                         | Missing: reads controllers in process_api cgroup too                             |
| 4.3  | S        | `"[DEBUG] Enabled memory controller in process_api cgroup"`                                            | Missing: separate enable step for process_api subtree                            |
| 4.4  | S        | `"[DEBUG] Failed to enable memory controller in process_api cgroup: "`                                 | Missing error path                                                               |
| 4.5  | S        | `"[DEBUG] memory controller already enabled in process_api cgroup"`                                    | Missing already-enabled check                                                    |
| 4.6  | F        | `"+ controller in root cgroup"` / `" controller already enabled in root cgroup"`                       | Different enable format (generic controller name)                                |
| 4.7  | S        | `"Cgroup v2 detected but not enabled. Please use --cgroupv2 flag..."`                                  | Missing: v2 detected but controllers not available                               |
| 4.8  | S        | `"/core"` / `"/proc/self/cgroup"` / `"/proc/self/mountinfo"`                                           | Missing: cgroup v2 nested detection via `/proc/self/cgroup` (systemd slice path) |
| 4.9  | S        | `"Direct creation succeeded"` / `"Direct creation failed: mkdir-p"` / `"Failed to create directory: "` | Missing: fallback cgroup creation with `mkdir -p`                                |
| 4.10 | S        | `"Memory limit not supported, container memory limits"`                                                | Missing: unsupported memory limit handling                                       |
| 4.11 | S        | `"Container memory limit not set, ignoring all limits"`                                                | Missing: log when no container memory limit                                      |
| 4.12 | S        | `"No controller found"`                                                                                | Missing: controller detection failure                                            |
| 4.13 | S        | `"Removed cgroup directory"` / `"Failed to remove cgroup directory: "`                                 | Missing: explicit cleanup logs                                                   |
| 4.14 | S        | `"cpu.cfs_period_us"` / `"/sys/fs/cgroup/cpu,cpuacct"`                                                 | Missing: v1 combined cpu,cpuacct controller support                              |
| 4.15 | S        | `"Cgroup is not ready"` (exact format)                                                                 | Different wording in wait loop                                                   |

### 5. `oom_killer.rs` — OOM monitoring

| #    | Severity | Reference string                                                                                   | Gap                                                                        |
| ---- | -------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 5.1  | F        | `"[DEBUG] container_oom_monitor: Container memory usage  exceeds limit "`                          | Different message format (RE: `"Container memory limit exceeded: usage="`) |
| 5.2  | S        | `"[DEBUG] container_oom_monitor: Adopting orphans before memory scan..."`                          | **Missing**: OOM monitor calls orphan adoption before scanning memory      |
| 5.3  | S        | `"[DEBUG] container_oom_monitor: Reading fresh memory usage for ALL processes to find largest..."` | Missing debug log before memory scan                                       |
| 5.4  | S        | `"[DEBUG] container_oom_monitor: Failed to adopt orphans: "`                                       | Missing orphan adoption error in OOM path                                  |
| 5.5  | F        | `"[DEBUG] container_oom_monitor: Killed process ) exited after s"`                                 | Missing: post-kill timing log                                              |
| 5.6  | F        | `"[DEBUG] container_oom_monitor: Memory reclaimed to ) s after kill"`                              | Different format (RE: `"Memory reclaimed after killing"`)                  |
| 5.7  | S        | `"[DEBUG] container_oom_monitor: Failed to notify kill for process "`                              | Missing: OOM channel send failure log                                      |
| 5.8  | S        | `"[DEBUG] Error getting container memory usage: "`                                                 | Missing: container-level memory read error                                 |
| 5.9  | S        | `"Memory limit not set, per process memory limits"`                                                | Missing: fallback behavior when no container limit                         |
| 5.10 | F        | `"[DEBUG] container_oom_monitor: Killing process  with memory usage  to free up memory"`           | Slightly different format                                                  |

### 6. `proc_handle.rs` — Process lifecycle

| #   | Severity | Reference string                                          | Gap                                             |
| --- | -------- | --------------------------------------------------------- | ----------------------------------------------- |
| 6.1 | S        | `"[DEBUG] Killing process tree OOM killed process "`      | Missing: separate OOM kill tree message         |
| 6.2 | M        | `"[DEBUG] Failed to send OOM killed status for process "` | Missing: OOM status send failure log            |
| 6.3 | M        | `"[DEBUG] Failed to send timeout status for process "`    | Missing: timeout status send failure log        |
| 6.4 | F        | `"Error waiting for process group: "` (with space prefix) | Different format (RE includes "PID" in message) |

### 7. `adopter.rs` — Orphan adoption

| #   | Severity | Reference string                                               | Gap                                                         |
| --- | -------- | -------------------------------------------------------------- | ----------------------------------------------------------- |
| 7.1 | S        | `"[DEBUG] Error reading status for orphaned process "`         | Missing: different error message (uses "process" not "PID") |
| 7.2 | S        | `"[DEBUG] Found new zombie PID , will reap in next iteration"` | Missing: PID-level zombie tracking (not orphan-level)       |

### 8. `state.rs` — Process state management

| #   | Severity | Reference string                                            | Gap                                              |
| --- | -------- | ----------------------------------------------------------- | ------------------------------------------------ |
| 8.1 | S        | `" is not an OOM reason"`                                   | Missing: OOM reason validation                   |
| 8.2 | S        | `"[DEBUG] Failed to send OOM notification for process_id "` | Missing: OOM notification failure                |
| 8.3 | F        | `"process not found"` (lowercase)                           | RE has `"Process not found:"` — different casing |

### 9. Missing structural elements (from serde/debug field names)

These field names appear in the reference binary's string table, indicating
struct definitions that are serialized (serde) or debug-printed:

| Field name                                  | Implication                                                        |
| ------------------------------------------- | ------------------------------------------------------------------ |
| `ProcessInfo` / `process_info`              | A struct with richer process info (likely used in healthcheck)     |
| `internal_state`                            | Process entries have an internal state field beyond `ProcessState` |
| `ProcController` / `controller`             | Wrapper type around cgroup controller + channels                   |
| `WsStreamHandle` / `Mutex`                  | Dedicated WS stream handle type (not raw `SplitSink`)              |
| `memory_usage_bytes` / `memory_cgroup_path` | Additional fields on proc handle                                   |
| `oom_killed_tx` / `stop_waiting_rx`         | Channel fields with different naming                               |
| `proc_handle` (lowercase)                   | Field name in a container struct                                   |

These suggest the internal architecture is richer than what the RE currently models.

## Verification Strategy

### Level 1: String coverage

Extract all application-level strings from both binaries and diff. Every
`[DEBUG]`, `[CONTROL]`, `[SECURITY]`, `[ERROR]`, `[INFO]`, `[OOM_KILL]` format
string in the original must appear verbatim in our reconstruction.

### Level 2: Behavioral testing

Write a WebSocket test harness that exercises the protocol against both the
original and reconstruction:

1. `CreateProcess` -> stdout + `ProcessExited`
2. Wrong `expected_container_name` -> rejection
3. `ProcessConnection` reattach to detached process
4. `SendSignal` to running process -> `SignalSent`
5. Memory hog -> `ProcessOutOfMemory` / `ContainerOutOfMemory`
6. `StdInEOF` -> stdin closes
7. Local IP with `--block-local-connections` -> rejected

### Level 3: Address-level traceability

Every function in the reconstruction has a `/// Decompiled from 0xAAAA..0xBBBB`
comment. This lets anyone verify any function by opening the binary at that
address in Ghidra and comparing the logic.

### Level 4: Section size comparison

Compile with the same rustc (1.83.0) and crate versions. `.text` section size
should be within ~20% of the original.

## Remediation Plan

### Phase A: Fix format strings (F-severity, ~20 items)

Fix all wrong-wording format strings. These are one-line fixes:

1. `main.rs:170` — `"release "` → `"release: "` (add colon)
2. `main.rs:171` — `"version "` → `"version: "` (add colon)
3. `main.rs:249` — `"Caught signal SIGINT!"` → `"[DEBUG] Received SIGINT, initiating shutdown"`
4. `cgroup.rs:109,111` — Reformat root controller logs to `"root: current_controllers:"` / `"root: subtree_control:"`
5. `cgroup.rs` — Generic controller name in enable messages
6. `oom_killer.rs:164` — Reformat OOM exceeded message
7. `oom_killer.rs:274-276` — Reformat memory reclaimed message
8. `proc_handle.rs:162` — Reformat error-waiting message
9. `state.rs:58` — Lowercase `"process not found"` in one code path

### Phase B: Add missing log messages (M-severity, ~10 items)

Add missing debug logs that witness existing code paths:

1. `io.rs` — Add `process_ws_message returned:` / `failed:` logs
2. `io.rs` — Add `Stopping waiting for process` log
3. `io.rs` — Add `WebSocket closed, remaining()` log
4. `main.rs` — Add `with web socket buffer size of` startup log
5. `main.rs` — Add `got shutdown channel rx` log
6. `control_server.rs` — Add `Control server shutdown complete` log
7. `proc_handle.rs` — Add `Failed to send OOM killed/timeout status` logs

### Phase C: Add missing code paths (S-severity, ~30 items)

These are the substantive gaps requiring new logic. Prioritized by impact:

**C1. `io.rs` restructuring (highest impact)**

The WS message processing loop needs refactoring:

- Extract `process_ws_message` as a separate named function
- Extract `forward_stdin` as a separate spawned task
- Add `ExpectStdIn` → binary message protocol enforcement
- Add explicit WS close on cleanup with error handling
- Add `process_ws_message: Timeout` / `OOM` / `Container OOM` handling
- Add `Failed to send response:` error handling on all `send_msg` calls
- Add `Failed to receive message:` error path
- Add `error closing websocket:` cleanup
- Add `Process stream is closed` detection

**C2. `cgroup.rs` enrichment (high impact)**

- Add v2 nested cgroup detection via `/proc/self/cgroup` (systemd slice)
- Add `/proc/self/mountinfo` parsing for cgroup mount detection
- Add fallback cgroup creation with `mkdir -p` subprocess
- Add `cpu.cfs_period_us` and `/sys/fs/cgroup/cpu,cpuacct` v1 support
- Add controller availability check in process_api subtree
- Add `Memory limit not supported` / `Container memory limit not set` handling
- Add explicit cleanup logs (`Removed cgroup directory` / `Failed to remove`)

**C3. `control_server.rs` enrichment (medium impact)**

- Add local IP rejection on control server connections
- Enrich healthcheck with `/proc` readings:
  - Process limit from `/proc/self/limits`
  - Total system processes from `/proc` PID enumeration
  - PID max from `/proc/sys/kernel/pid_max`
- Add `Control server shutdown complete` log

**C4. `oom_killer.rs` enrichment (medium impact)**

- Add orphan adoption step before memory scanning in container OOM monitor
- Add `Reading fresh memory usage for ALL processes` log
- Add post-kill timing (`exited after Xs`, `Memory reclaimed to X`)
- Add `Failed to notify kill` error path
- Add container memory read error handling
- Add fallback for missing container memory limit

**C5. `main.rs` enrichment (medium impact)**

- Add `[SECURITY] Blocking connections from local IPs:` startup log
- Add `Performing graceful shutdown...` / `All connections and monitors closed...` messages
- Add WS buffer size startup log

**C6. `state.rs` / `adopter.rs` / `proc_handle.rs` (low impact)**

- Add OOM reason validation (`is not an OOM reason`)
- Add OOM notification failure log
- Add PID-level zombie tracking in adopter
- Add `Killing process tree OOM killed process` log

**C7. Structural type enrichment (low priority, high effort)**

The serde field names suggest richer internal types:

- `ProcessInfo` struct for healthcheck serialization
- `ProcController` wrapper type
- `WsStreamHandle` type
- Additional `memory_usage_bytes` / `memory_cgroup_path` fields

These don't affect behavioral correctness but would make the string diff
cleaner. Defer unless aiming for a near-perfect string match.

### Phase D: Re-run string differential

After all fixes, rebuild and re-run the string diff. Target: zero missing
application-level strings (excluding library/compiler differences).

### Phase E: Behavioral testing

Build the WebSocket test harness and run against both binaries.

## Status

- [x] Install Ghidra headless + run full binary analysis
- [x] Generate function catalog (address -> source file mapping via string xrefs)
- [x] Bazel build setup (`BUILD.bazel`, deps in `Cargo.toml`)
- [x] Decompile and translate each module:
  - [x] `pid_tree.rs`
  - [x] `control_server.rs`
  - [x] `cgroup.rs`
  - [x] `state.rs`
  - [x] `adopter.rs`
  - [x] `proc_handle.rs`
  - [x] `oom_killer.rs`
  - [x] `main.rs`
  - [x] `io.rs`
- [x] Full build compiles
- [x] String differential analysis run (2026-02-13)
- [ ] Remediation:
  - [ ] Phase A: Fix format strings (~20 items)
  - [ ] Phase B: Add missing log messages (~10 items)
  - [ ] Phase C: Add missing code paths (~30 items)
    - [ ] C1: `io.rs` restructuring
    - [ ] C2: `cgroup.rs` enrichment
    - [ ] C3: `control_server.rs` enrichment
    - [ ] C4: `oom_killer.rs` enrichment
    - [ ] C5: `main.rs` enrichment
    - [ ] C6: `state.rs` / `adopter.rs` / `proc_handle.rs`
    - [ ] C7: Structural type enrichment (deferred)
  - [ ] Phase D: Re-run string differential (target: clean diff)
- [ ] Verification:
  - [ ] String coverage diff passes
  - [ ] Behavioral test harness written
  - [ ] Behavioral tests pass against both binaries
  - [x] Every function has `Decompiled from 0x...` annotation
