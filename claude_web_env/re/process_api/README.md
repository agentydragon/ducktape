# process_api Reverse Engineering

Reverse-engineered source code for `process_api`, Anthropic's container init
process (PID 1) for Claude Code web containers. The binary manages process
lifecycles over WebSocket, handles cgroup-based resource limits, and performs
PID 1 duties (orphan adoption, zombie reaping).

## Target Binary

| Property            | Value                                      |
| ------------------- | ------------------------------------------ |
| **Release**         | `process_api_2026-02-02-04-57`             |
| **Package version** | `0.1.0`                                    |
| **ELF Build ID**    | `b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0` |
| **Reference file**  | `claude_web_env/reference/process_api.gz`  |
| **Language**        | Rust                                       |
| **Compiler**        | `rustc 1.83.0 (90b35a623 2024-11-26)`      |
| **Stripped**        | Yes (no debug info, no symbol table)       |
| **Binary size**     | ~912 KB (gzipped)                          |
| **Functions**       | 2,382 total (29 application, rest stdlib)  |

Reconstructed source lives under `b0e4b2f4/` (Build ID prefix), so multiple
binary versions can coexist.

## Build

```bash
bazel build //claude_web_env/re/process_api/b0e4b2f4:process_api_re
```

## Approach

Decompilation-first using Ghidra headless:

1. **Full Ghidra decompilation** of the stripped ELF binary (2,382 functions)
2. **String cross-references** mapped ~200 application strings to their source
   files via `/build/src/*.rs` panic paths, producing a function catalog of
   29 application functions across 9 source files
3. **Translation** of Ghidra's decompiled C pseudocode to idiomatic Rust, guided
   by known types (serde field names, clap struct, message enums)
4. **Assembly** into the original 9-file module structure with Bazel build
5. **String differential analysis** (Phases A-D) to close ~60 gaps in format
   strings, debug logs, and missing code paths
6. **Structural type enrichment** (Phase C7) to match serde field names from
   the binary's serialization visitors

Every function is annotated with `/// Decompiled from 0xAAAA..0xBBBB` and
`/// Xrefs:` referencing the binary address range and string cross-references,
so the reconstruction is auditable against the original.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    process_api (PID 1)                    │
│                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────┐  │
│  │  WebSocket    │    │   Control    │    │  Orphan    │  │
│  │  Listener     │    │   Server    │    │  Monitor   │  │
│  │  (port 2024)  │    │  (port 2025) │    │  (5s poll) │  │
│  └──────┬───────┘    └──────┬───────┘    └─────┬──────┘  │
│         │                   │                  │         │
│  ┌──────▼───────┐    ┌──────▼───────┐    ┌─────▼──────┐  │
│  │   io.rs      │    │control_srv.rs│    │ adopter.rs │  │
│  │ WS protocol  │    │  HTTP API    │    │ zombie     │  │
│  │ I/O forward  │    │  shutdown    │    │ reaping    │  │
│  └──────┬───────┘    └──────────────┘    └────────────┘  │
│         │                                                │
│  ┌──────▼───────┐    ┌──────────────┐    ┌────────────┐  │
│  │ proc_handle  │    │  cgroup.rs   │    │oom_killer  │  │
│  │ lifecycle    │◄──►│  v1/v2 mgmt  │◄──►│ container  │  │
│  │ wait/kill    │    │  mem/cpu     │    │ + per-proc │  │
│  └──────┬───────┘    └──────────────┘    └────────────┘  │
│         │                                                │
│  ┌──────▼───────┐    ┌──────────────┐                    │
│  │  state.rs    │    │ pid_tree.rs  │                    │
│  │ process map  │    │ /proc reader │                    │
│  │ attach/detach│    │ descendants  │                    │
│  └──────────────┘    └──────────────┘                    │
└──────────────────────────────────────────────────────────┘
```

`process_api` runs as PID 1 in the container. It exposes two network
interfaces:

- **WebSocket listener** (typically port 2024): Accepts connections from
  `environment-manager` to spawn and manage child processes.
- **HTTP control server** (typically port 2025): Accepts connections from the
  orchestration layer for graceful shutdown and container metadata updates.

Internally it runs several concurrent tasks:

- **Orphan monitor** (5-second polling): Adopts orphaned processes and reaps
  zombies, as required of PID 1.
- **Container OOM monitor** (configurable polling, default 100ms): Watches
  container-level cgroup memory and kills the largest process when exceeded.
- **Per-process OOM monitors**: One per process with a memory limit, watching
  individual cgroup usage.

## Module Breakdown

### Source Files (9 modules)

| Module              | Lines | Purpose                                 |
| ------------------- | ----- | --------------------------------------- |
| `main.rs`           | 389   | CLI, cgroup init, WS listener, shutdown |
| `io.rs`             | 1063  | WebSocket protocol, process I/O         |
| `state.rs`          | 164   | Process map state machine               |
| `proc_handle.rs`    | 413   | Per-process lifecycle, kill/wait        |
| `cgroup.rs`         | 385   | Cgroup v1/v2 setup, memory/CPU          |
| `control_server.rs` | 382   | HTTP control API                        |
| `oom_killer.rs`     | 381   | Container + per-process OOM monitors    |
| `adopter.rs`        | 225   | Orphan adoption, zombie reaping         |
| `pid_tree.rs`       | 61    | `/proc` PID tree traversal              |

### Function Address Map

Key functions with their binary address ranges (for cross-referencing with
Ghidra):

| Function                     | Address range        | Size    | Module              |
| ---------------------------- | -------------------- | ------- | ------------------- |
| `main` (async entry)         | `0x2273c0..0x232177` | 44.5 KB | `main.rs`           |
| CLI builder                  | `0x209200..0x20ca80` | 14.5 KB | `main.rs`           |
| CLI parser/init              | `0x20d0c0..0x21199e` | 18.7 KB | `main.rs`           |
| Container name detection     | `0x2089f0..0x2091fe` | 2.1 KB  | `main.rs`           |
| Socket bind/listen           | `0x13f6a0..0x13faff` | 1.1 KB  | `main.rs`           |
| `CreateProcess` deserializer | `0x233900..0x23567c` | 7.5 KB  | `io.rs`             |
| Stdout pipe handler          | `0x144970..0x145eb0` | 5.4 KB  | `io.rs`             |
| Stderr pipe handler          | `0x141db0..0x1432f0` | 5.4 KB  | `io.rs`             |
| Exit status formatter        | `0x1bafc0..0x1bb772` | 2.0 KB  | `io.rs`             |
| WS message enum deserializer | `0x1275e0..0x12766e` | 142 B   | `io.rs`             |
| State map lookup             | `0x1ba610..0x1bacf7` | 1.8 KB  | `state.rs`          |
| State transition validate    | `0x1b9f30..0x1ba60d` | 1.8 KB  | `state.rs`          |
| `kill_and_wait`              | `0x1b5620..0x1b5b56` | 1.3 KB  | `proc_handle.rs`    |
| `ProcessInfo` deserializer   | `0x21c970..0x21cb45` | 469 B   | `proc_handle.rs`    |
| `CgroupConfig` deserializer  | `0x21ce40..0x21d015` | 469 B   | `proc_handle.rs`    |
| `ProcHandle` deserializer    | `0x21d120..0x21d303` | 483 B   | `proc_handle.rs`    |
| `detect_cgroup_version`      | `0x1b4f20..0x1b5085` | —       | `cgroup.rs`         |
| `setup_cgroup_path`          | `0x1b50e0..0x1b54b5` | —       | `cgroup.rs`         |
| `setup_cgroup`               | `0x1b5df0..0x1b729c` | —       | `cgroup.rs`         |
| `read_memory_usage`          | `0x1328a0..0x132d66` | —       | `cgroup.rs`         |
| Connection handler           | `0x143330..0x14496f` | 5.7 KB  | `control_server.rs` |
| Control startup/shutdown     | `0x1471a0..0x14796d` | 2.0 KB  | `control_server.rs` |
| Healthcheck builder          | `0x0fef20..0x10032a` | 1.0 KB  | `control_server.rs` |
| OOM event handler setup      | `0x21c2b0..0x21c510` | 608 B   | `oom_killer.rs`     |
| OOM killed TX setup          | `0x21c870..0x21c96b` | 251 B   | `oom_killer.rs`     |

## CLI Arguments

```
process_api [OPTIONS] --addr <ADDR>

Options:
  --addr <ADDR>                    WebSocket listen address (required, e.g., "0.0.0.0:2024")
  --max-ws-buffer-size <SIZE>      WebSocket frame buffer size [default: 32768]
  --memory-limit-bytes <BYTES>     Container-level memory limit (enables OOM monitor)
  --cpu-shares <SHARES>            CPU weight — cgroup v1 cpu.shares or v2 cpu.weight
  --oom-polling-period-ms <MS>     OOM check interval [default: 100]
  --cgroupv2                       Force cgroup v2 mode (auto-detected otherwise)
  --control-server-addr <ADDR>     HTTP control server (e.g., "0.0.0.0:2025")
                                   When set, SIGINT handler is disabled
  --block-local-connections        Reject 127.0.0.1, ::1, 0.0.0.0, :: on both servers
```

All flags accept corresponding `SCREAMING_SNAKE_CASE` environment variables
(e.g., `MEMORY_LIMIT_BYTES`, `CONTROL_SERVER_ADDR`).

## WebSocket Protocol

Clients connect via WebSocket and send a JSON text message as the first frame:
either a `CreateProcess` (spawn a new process) or a `ProcessConnection`
(reattach to a detached process). Server responds with tagged JSON messages
(`{"type": "ProcessCreated", ...}`). Stdout/stderr are sent as
`ExpectStdOut`/`ExpectStdErr` text frames followed by binary data frames.
Stdin uses `ExpectStdIn` + binary frame.

### First Message: `CreateProcess`

Spawn a new child process. The `name` field doubles as the `process_id` key.

```json
{
  "name": "/bin/bash",
  "args": ["-l"],
  "env_vars": { "TERM": "xterm-256color" },
  "clear_env": false,
  "uid": 1000,
  "gid": 1000,
  "reattachable": true,
  "allow_process_id_reuse": false,
  "timeout": 300,
  "memory_limit_bytes": 1073741824
}
```

| Field                    | Type                | Default | Description                                  |
| ------------------------ | ------------------- | ------- | -------------------------------------------- |
| `name`                   | `string`            | —       | Command to execute (also used as process ID) |
| `args`                   | `string[]`          | —       | Command arguments                            |
| `env_vars`               | `{string: string}?` | `null`  | Additional environment variables             |
| `clear_env`              | `bool?`             | `false` | Clear inherited environment                  |
| `uid`                    | `u32?`              | —       | Run as this UID                              |
| `gid`                    | `u32?`              | —       | Run as this GID                              |
| `reattachable`           | `bool?`             | `false` | Keep process alive on WS disconnect          |
| `allow_process_id_reuse` | `bool?`             | `true`  | Allow reusing an existing process ID         |
| `timeout`                | `u64?`              | —       | Kill after N seconds                         |
| `memory_limit_bytes`     | `u64?`              | —       | Per-process memory limit via cgroup          |

The spawned process runs in a new session (`setsid`), with piped
stdin/stdout/stderr. If `memory_limit_bytes` is set, a per-process cgroup is
created under `/sys/fs/cgroup/process_api/{pid}/`.

### First Message: `ProcessConnection`

Reattach to a previously detached process, or query its state.

```json
{
  "process_id": "/bin/bash",
  "reattach": true,
  "expected_container_name": "my-container"
}
```

| Field                     | Type      | Default | Description                            |
| ------------------------- | --------- | ------- | -------------------------------------- |
| `process_id`              | `string`  | —       | ID of process to reconnect to          |
| `reattach`                | `bool?`   | `true`  | Actually reattach (false = just query) |
| `expected_container_name` | `string?` | —       | Validate container identity            |

If `expected_container_name` is set and doesn't match the container's current
name, the server responds with `InfraError` and closes.

### Client-to-Server Messages

After the first frame, the client sends tagged JSON text messages:

```json
{"type": "SendSignal", "signal": "SIGTERM"}
{"type": "ExpectStdIn"}
{"type": "StdInEOF"}
```

| Message       | Fields           | Description                                      |
| ------------- | ---------------- | ------------------------------------------------ |
| `SendSignal`  | `signal: string` | Signal name or number (e.g., `"SIGKILL"`, `"9"`) |
| `ExpectStdIn` | —                | Next binary frame is stdin data                  |
| `StdInEOF`    | —                | Close the child's stdin pipe                     |

Supported signals: `SIGHUP`, `SIGINT`, `SIGQUIT`, `SIGKILL`, `SIGTERM`,
`SIGUSR1`, `SIGUSR2`, `SIGCONT`, `SIGSTOP`. Numeric values also accepted.

### Server-to-Client Messages

All responses are tagged JSON text messages (`{"type": "...", ...}`):

| Message                  | Fields                           | Description                            |
| ------------------------ | -------------------------------- | -------------------------------------- |
| `ProcessCreated`         | `process_id`, `pid`              | Process spawned successfully           |
| `AttachedToProcess`      | `process_id`, `pid`              | Reattached to detached process         |
| `ProcessNotRunning`      | `process_id`                     | Process not found or already exited    |
| `ProcessAlreadyAttached` | `process_id`                     | Another WS is attached to this process |
| `FailedToStartProcess`   | `error`                          | Spawn failed                           |
| `WithSameIdRunning`      | `process_id`                     | Duplicate ID (and reuse disallowed)    |
| `InfraError`             | `error`                          | Infrastructure error (name mismatch)   |
| `ExpectStdOut`           | —                                | Next binary frame is stdout data       |
| `StdOutEOF`              | —                                | Stdout pipe closed                     |
| `ExpectStdErr`           | —                                | Next binary frame is stderr data       |
| `StdErrEOF`              | —                                | Stderr pipe closed                     |
| `ProcessExited`          | `status: i32`, `details: string` | Normal exit or signal death            |
| `ProcessTimedOut`        | `timeout_secs`, `details`        | Killed after timeout exceeded          |
| `ProcessOutOfMemory`     | `limit_bytes`, `details`         | Per-process memory limit exceeded      |
| `ContainerOutOfMemory`   | `limit_bytes`, `details`         | Container-level OOM kill               |
| `InvalidSignal`          | `signal`                         | Unrecognized signal name/number        |
| `FailedToSendSignal`     | `error`                          | Signal delivery failed                 |
| `SignalSent`             | `signal`                         | Signal delivered successfully          |
| `ShuttingDown`           | —                                | Server is shutting down                |

### I/O Forwarding Sequence

```
Server                              Client
  │                                   │
  │◄── CreateProcess (JSON text) ─────│
  │                                   │
  │── ProcessCreated (JSON text) ────►│
  │                                   │
  │── ExpectStdOut (JSON text) ──────►│  ┐
  │── [stdout bytes] (binary) ───────►│  ├ repeats
  │── ExpectStdErr (JSON text) ──────►│  │
  │── [stderr bytes] (binary) ───────►│  ┘
  │                                   │
  │◄── ExpectStdIn (JSON text) ───────│  ┐
  │◄── [stdin bytes] (binary) ────────│  ├ repeats
  │◄── StdInEOF (JSON text) ──────────│  ┘
  │                                   │
  │── StdOutEOF (JSON text) ─────────►│
  │── StdErrEOF (JSON text) ─────────►│
  │── ProcessExited (JSON text) ─────►│
```

Binary frames are read in 64 KB chunks. Each chunk is preceded by an
`ExpectStdOut`/`ExpectStdErr` text frame signaling which stream follows.

## HTTP Control Server

When `--control-server-addr` is set, the SIGINT handler is disabled and
shutdown is driven exclusively through HTTP.

| Method | Path              | Request body      | Response                           |
| ------ | ----------------- | ----------------- | ---------------------------------- |
| `POST` | `/shutdown`       | —                 | `200 "Shutdown initiated\n"`       |
| `POST` | `/container_name` | UTF-8 name string | `200 "Container name set to: X\n"` |
| `GET`  | `/health`         | —                 | `200 "OK\n"`                       |
| `GET`  | `/healthcheck`    | —                 | `200` diagnostic text (see below)  |
| `GET`  | `/container_name` | —                 | `200 "X\n"` or `"not set\n"`       |
| `*`    | `*`               | —                 | `404 "Not Found\n"`                |

**`POST /shutdown`** performs `sync(1)` before sending the broadcast shutdown
signal. All tracked processes are then killed.

**`GET /healthcheck`** returns a multi-line diagnostic string:

```
Currently tracked processes: N
  proc_id: pid=PID, state=STATE, reattachable=BOOL
Process limit (soft/hard): SOFT/HARD
Total system processes: COUNT
System PID max: MAX
Diagnostic info: [OK
```

Data sources: tracked process map, per-process cgroup memory usage,
`/proc/self/limits` (RLIMIT_NPROC), `/proc/sys/kernel/pid_max`,
`ps aux --no-headers`.

**Security**: Both the control server and WebSocket listener reject connections
from local IPs (127.0.0.1, ::1, 0.0.0.0, ::) when `--block-local-connections`
is set. The control server additionally rejects local IPs unconditionally.

## Cgroup Resource Management

`process_api` auto-detects cgroup v1 vs v2 and creates a `process_api/`
hierarchy for managed processes.

### Version Detection

1. Check `/sys/fs/cgroup/cgroup.controllers` — if present with non-empty
   content, use **v2**
2. Fall back to `/sys/fs/cgroup/memory` — if present, use **v1**
3. `--cgroupv2` flag forces v2 regardless

### Hierarchy Setup

**v1**: `/sys/fs/cgroup/memory/process_api/`
**v2**: `/sys/fs/cgroup/process_api/` (or nested path detected from
`/proc/self/cgroup` for systemd-managed containers)

Setup sequence:

1. Create `process_api/` directory (with `mkdir -p` fallback)
2. (v2) Enable `memory` + `pids` controllers in `cgroup.subtree_control`
3. Set `cgroup.procs` permissions to `0o666` (allows unprivileged process
   self-addition)
4. Move PID 1 into the cgroup

Per-process cgroups are created as `process_api/{pid}/` subdirectories when a
`CreateProcess` request includes `memory_limit_bytes`.

### Resource Controls

| Resource     | v1 file                                                                    | v2 file          |
| ------------ | -------------------------------------------------------------------------- | ---------------- |
| Memory limit | `memory.limit_in_bytes`                                                    | `memory.max`     |
| Memory usage | `memory.usage_in_bytes`                                                    | `memory.current` |
| CPU weight   | `/sys/fs/cgroup/cpu,cpuacct/cpu.shares` or `/sys/fs/cgroup/cpu/cpu.shares` | `cpu.weight`     |

### Cgroup Setup Retry

If cgroup setup fails (e.g., filesystem not yet mounted), `process_api` retries
in a loop with 10-second backoff until successful.

## OOM Monitoring

Two independent OOM monitoring systems run concurrently:

### Container-Level OOM Monitor

Enabled when `--memory-limit-bytes` is set. Polls the container cgroup's
`memory.current`/`memory.usage_in_bytes` at `--oom-polling-period-ms` intervals.

When usage exceeds the limit:

1. **Adopt orphans** first (via `try_adopt_orphans`) to ensure accurate
   process tracking
2. **Scan all process cgroups** to find the process with the highest memory
   usage
3. **Read `/proc/{pid}/cmdline`** for logging
4. **Write OOM kill event** to `/var/log/.process_api/oom_killed.log`:
   ```
   [OOM_KILL] process_id=X pid=Y memory_bytes=Z limit_bytes=L reason=container_limit cmdline=...
   ```
5. **Signal the process** via its OOM channel (if registered) or fall back to
   direct `kill_and_wait`
6. **Post-kill wait** (up to 30 seconds in two phases):
   - Phase 1 (10s): Wait for PID to disappear from `/proc`
   - Phase 2 (20s): Wait for container memory to drop below limit

### Per-Process OOM Monitor

One task per process with `memory_limit_bytes` set. Polls the individual
cgroup's memory usage. When exceeded, signals the process's OOM channel. The
`wait_for_child_to_exit` task receives the signal and kills the process tree.

### OOM Channel Registry

Both monitors communicate via a shared `OomChannelMap`:
`Arc<Mutex<HashMap<String, oneshot::Sender<()>>>>`. When a process is spawned
with a memory limit, its OOM channel sender is registered. When the OOM monitor
fires, it removes and sends on the channel, which wakes the
`wait_for_child_to_exit` task to perform the actual kill.

## Process State Machine

Each managed process is tracked in a shared `ProcessMap` with three states:

```
                 ┌──────────┐
    insert ────► │ Attached │ ◄──── attach (from Detached)
                 └────┬─────┘
                      │
              ┌───────┴───────┐
              ▼               ▼
        ┌──────────┐    ┌──────────┐
        │ Detached │    │   Done   │ ──── remove from map
        └──────────┘    └──────────┘
```

- **Attached**: WebSocket is actively connected, I/O is flowing
- **Detached**: Process is running but the WS client disconnected
  (only if `reattachable=true`)
- **Done**: Process has exited, handle is being cleaned up

State transitions are validated — attempting an invalid transition (e.g.,
`Attached → Attached`) logs an inconsistency error. The state check uses
an explicit `expected_state` parameter to detect races.

### Process Lifecycle

1. **Spawn**: `CreateProcess` → command is spawned → state set to `Attached`
2. **I/O forwarding**: Stdout/stderr piped through WebSocket binary frames
3. **Exit detection**: `wait_for_child_to_exit` polls `waitpid(WNOHANG)` at
   50ms intervals, checking for:
   - Process exit (normal or signal)
   - Timeout expiry
   - Per-process memory limit exceeded
   - Container OOM notification (via channel)
   - Stop signal (from shutdown or non-reattachable disconnect)
4. **Cleanup**: Kill process tree (`SIGKILL` to process group + all
   descendants), wait up to 30s, remove cgroup directory

### Kill Sequence

`kill_and_wait(pid, cgroup_path)`:

1. `killpg(pid, SIGKILL)` — kill entire process group
2. Fall back to `kill(pid, SIGKILL)` if `killpg` fails
3. Kill all descendants found via `/proc/{pid}/task/*/children` traversal
4. `waitpid(pid, WNOHANG)` loop with 10ms sleep (30s timeout)
5. Poll `cgroup.procs` to verify all processes exited
6. Remove cgroup directory

## PID 1 Duties

### Orphan Adoption (5-second poll)

As PID 1, `process_api` inherits orphaned processes. The orphan monitor:

1. **Cleans up tracked zombies** that no longer exist in `/proc`
2. **Reaps zombies** via `waitpid(-1, WNOHANG)` loop
3. **Discovers orphans**: Gets child PIDs of PID 1, lists all managed cgroups,
   finds PIDs not in any managed cgroup
4. **Classifies**: Zombies are tracked for reaping; live orphans are moved into
   the `process_api` base cgroup

### Zombie Reaping

Uses `waitpid(-1, WNOHANG)` in a loop to reap all available zombie children.
Tracked zombies log their age when finally reaped.

## Startup Sequence

1. Initialize `env_logger`
2. Parse CLI arguments
3. Log version: `[INFO] process_api release: process_api_2026-02-02-04-57`
4. Set up cgroup hierarchy (with retry loop on failure, 10s backoff)
5. Set CPU shares if configured
6. Detect container name from `/container_info.json`
7. Start control server OR SIGINT handler (mutually exclusive)
8. Start orphan monitor task
9. Start container OOM monitor task (if memory limit set)
10. Bind WebSocket listener, enter accept loop
11. On shutdown signal: kill all tracked processes, log completion

## Container Integration

In the live Claude Code web container, `process_api` is invoked as:

```
/process_api --addr 0.0.0.0:2024 \
  --control-server-addr 0.0.0.0:2025 \
  --memory-limit-bytes <container_limit> \
  --block-local-connections
```

`environment-manager` (the next binary in the boot chain) connects via
WebSocket to port 2024 to spawn the Claude Code agent process. The
orchestration layer connects to port 2025 for lifecycle management.

See <../../docs/environment_discovery.md> for the full container boot sequence
and how `process_api` fits into the `process_api → environment-manager →
claude` process tree.

## Dependencies

| Crate                  | Purpose                                  |
| ---------------------- | ---------------------------------------- |
| `tokio`                | Async runtime                            |
| `tokio-tungstenite`    | WebSocket server                         |
| `hyper` + `hyper-util` | HTTP/1.1 control server                  |
| `http-body-util`       | HTTP body handling                       |
| `serde` + `serde_json` | JSON serialization/deserialization       |
| `clap`                 | CLI argument parsing                     |
| `nix`                  | Unix syscalls (signals, waitpid, setsid) |
| `parking_lot`          | Synchronous mutex (for process map)      |
| `futures`              | Stream/Sink extensions for WebSocket     |
| `bytes`                | Byte buffer utilities                    |
| `log` + `env_logger`   | Logging                                  |

### Dependency Version Drift

The reconstructed binary uses newer crate versions than the original:

| Crate         | Original | Reconstructed | Impact     |
| ------------- | -------- | ------------- | ---------- |
| `tungstenite` | 0.24     | 0.28          | API compat |
| `nix`         | 0.29     | 0.31          | API compat |
| `clap_lex`    | 0.7      | 1.0           | Internal   |

These produce string differences in the binary (library panic paths, version
strings) but have no behavioral impact.

## Verification Status

See <b0e4b2f4/PLAN.md> for detailed verification work items.

- [x] Binary analysis, decompilation, translation, build
- [x] String differential analysis + remediation (Phases A-D)
- [x] String coverage diff passes (application-level strings)
- [x] Every function annotated with `Decompiled from 0x...`
- [x] Structural type enrichment (Phase C7)
- [ ] Behavioral test harness written
- [ ] Behavioral tests pass against both binaries
