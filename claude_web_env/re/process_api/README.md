# process_api Reverse Engineering

Reverse-engineered source code for `process_api`, Anthropic's container init
process (PID 1) for Claude Code web containers. The binary manages process
lifecycles over WebSocket, handles cgroup-based resource limits, and performs
PID 1 duties (orphan adoption, zombie reaping).

## Target Binary

| Property              | Value                                      |
| --------------------- | ------------------------------------------ |
| **Release**           | `process_api_2026-02-02-04-57`             |
| **Package version**   | `0.1.0`                                    |
| **ELF Build ID**      | `b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0` |
| **Reference file**    | `claude_web_env/reference/process_api.gz`  |
| **Language**          | Rust                                       |
| **Compiler**          | `rustc 1.83.0 (90b35a623 2024-11-26)`      |
| **Linking**           | Dynamic (libc, libm, libgcc_s)             |
| **Stripped**          | Yes (no debug info, no symbol table)       |
| **`.text` range**     | `0x1d060`..`0x1a8d0c` (~1.6 MB)            |
| **Total binary size** | 2,087,504 bytes (~2.0 MB)                  |

## Directory Layout

Reconstructed source lives under `b0e4b2f4/` (Build ID prefix), so multiple
binary versions can coexist.

```
process_api/
├── README.md              # This file
└── b0e4b2f4/              # Build ID b0e4b2f428d0...
    ├── BUILD.bazel
    ├── PLAN.md            # Remaining verification work
    └── src/
        ├── main.rs          # CLI, WS listener, signal handling, startup
        ├── io.rs            # WS protocol, serde structs, stdin/stdout/stderr
        ├── cgroup.rs        # Cgroup v1/v2 management
        ├── state.rs         # Process map, attach/detach state machine
        ├── proc_handle.rs   # Per-process lifecycle (wait, timeout, memory)
        ├── oom_killer.rs    # Container-level OOM monitor
        ├── adopter.rs       # Orphan adoption, zombie reaping
        ├── control_server.rs # HTTP control server
        └── pid_tree.rs      # /proc PID tree reader
```

## Build

```bash
bazel build //claude_web_env/re/process_api/b0e4b2f4:process_api_re
```

Dependencies are in the root `Cargo.toml`, pinned via
`CARGO_BAZEL_REPIN=1 bazel build @crates//:all`.

## Approach

Decompilation-first using Ghidra headless:

1. **Full Ghidra decompilation** of the stripped ELF binary (2,382 functions)
2. **String cross-references** mapped ~200 application strings to their source
   files via `/build/src/*.rs` panic paths, producing a function catalog of
   29 application functions across 9 source files
3. **Translation** of Ghidra's decompiled C pseudocode to idiomatic Rust, guided
   by known types (serde field names, clap struct, message enums)
4. **Assembly** into the original 9-file module structure with Bazel build

Every function is annotated with `/// Decompiled from 0xAAAA..0xBBBB` and
`/// Xrefs:` referencing the binary address range and string cross-references,
so the reconstruction is auditable against the original.

## Extracted Protocol

### WebSocket Protocol

Clients connect via WebSocket and send a JSON text message as the first frame:

- **`CreateProcess`** (10 fields): `name`, `args`, `env_vars`, `clear_env`,
  `uid`, `gid`, `reattachable`, `allow_process_id_reuse`, `timeout`,
  `memory_limit_bytes`
- **`ProcessConnection`** (3 fields): `process_id`, `reattach`,
  `expected_container_name`

Server responds with tagged JSON messages (`{"type": "ProcessCreated", ...}`).
Stdout/stderr are sent as `ExpectStdOut`/`ExpectStdErr` text frames followed
by binary data frames. Stdin uses `ExpectStdIn` + binary frame.

### Server-to-Client Messages (19 types)

`ProcessCreated`, `AttachedToProcess`, `ProcessNotRunning`,
`ProcessAlreadyAttached`, `FailedToStartProcess`, `WithSameIdRunning`,
`InfraError`, `ExpectStdOut`, `StdOutEOF`, `ExpectStdErr`, `StdErrEOF`,
`ProcessExited`, `ProcessTimedOut`, `ProcessOutOfMemory`,
`ContainerOutOfMemory`, `InvalidSignal`, `FailedToSendSignal`, `SignalSent`,
`ShuttingDown`

### Client-to-Server Messages (3 types)

`SendSignal`, `ExpectStdIn`, `StdInEOF`

### CLI Arguments

```
--addr                   WebSocket listen address (required)
--max-ws-buffer-size     Default 32768
--memory-limit-bytes     Container memory limit
--cpu-shares             CPU shares/weight
--oom-polling-period-ms  Default 100
--cgroupv2               Force cgroup v2
--control-server-addr    HTTP control server (disables SIGINT handler)
--block-local-connections  Reject localhost connections
```

### Control Server Endpoints

- `POST /shutdown` -- sync filesystem, send shutdown signal
- `POST /container_name` -- update container name
- `GET /healthcheck` -- diagnostic info

## Dependencies (from binary strings)

| Crate               | Version | Role                 |
| ------------------- | ------- | -------------------- |
| `tokio`             | 1.49.0  | Async runtime        |
| `tokio-tungstenite` | 0.24.0  | WebSocket server     |
| `hyper`             | 1.8.1   | HTTP server          |
| `clap`              | 4.5.56  | CLI argument parsing |
| `serde_json`        | 1.0.149 | JSON serialization   |
| `nix`               | 0.29.0  | Unix syscalls        |
| `parking_lot`       | 0.12.5  | Synchronization      |
| `futures-util`      | 0.3.31  | Stream combinators   |
| `bytes`             | 1.11.0  | Byte buffer          |

Plus 12 transitive dependencies (see <PLAN.md> for full list).

## Cgroup Paths

| Version | Memory usage                                              | Memory limit                            | CPU                                     |
| ------- | --------------------------------------------------------- | --------------------------------------- | --------------------------------------- |
| v1      | `/sys/fs/cgroup/memory/process_api/memory.usage_in_bytes` | `memory.limit_in_bytes`                 | `/sys/fs/cgroup/cpu/cpu.shares`         |
| v2      | `/sys/fs/cgroup/process_api/memory.current`               | `/sys/fs/cgroup/process_api/memory.max` | `/sys/fs/cgroup/process_api/cpu.weight` |
