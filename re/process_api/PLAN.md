# process_api Reverse Engineering Plan

## Target Binary

| Property                  | Value                                      |
| ------------------------- | ------------------------------------------ |
| **Release**               | `process_api_2026-02-02-04-57`             |
| **Package version**       | `0.1.0`                                    |
| **ELF Build ID**          | `b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0` |
| **SHA256 (decompressed)** | `066a048964945600...`                      |
| **SHA256 (.gz)**          | `10351f86963fb350...`                      |
| **Reference file**        | `claude_web_env/reference/process_api.gz`  |
| **Language**              | Rust                                       |
| **Compiler**              | `rustc 1.83.0 (90b35a623 2024-11-26)`      |
| **Linking**               | Dynamic (libc, libm, libgcc_s)             |
| **Stripped**              | Yes (no debug info, no symbol table)       |
| **`.text` size**          | 1,621,164 bytes (~1.6 MB)                  |
| **Total binary size**     | 2,087,504 bytes (~2.0 MB)                  |

## Goal

Produce compilable Rust source code that is **semantically equivalent** to the
original binary: same WebSocket protocol, same process management behavior, same
cgroup logic, same error handling paths. Not byte-identical — the goal is
readable, accurate source that could replace the binary.

## Approach: Decompilation with String-Guided Scaffolding

The binary contains ~200 unique application-level debug/error messages, exact
struct field names (from serde codegen), and full CLI definitions (from clap
derive). ~80% of the `.text` section is library code (tokio, hyper, tungstenite,
serde). The actual application logic is ~200-300 KB.

Strings give us the **skeleton** — module boundaries, data structures, message
types, error paths — but the actual **logic between log statements** requires
decompilation. You cannot reconstruct conditional branches, arithmetic, loop
bounds, or state machine transitions from log messages alone.

### Phase 1: Extract skeleton from strings

Identify all application-level strings and map them to source files. This gives
us:

- **Data structures**: Exact serde field names and counts, clap CLI struct
- **Module boundaries**: Which functions live in which file
- **Message protocol**: All 22 server→client and client→server message variants
- **Error paths**: Every error message implies a branch we need to reproduce
- **Filesystem interactions**: Exact cgroup paths, `/proc` paths, log paths

This phase produces a **skeleton** with correct types, correct module structure,
and placeholder `todo!()` bodies where logic needs to be filled in.

### Phase 2: Decompile and fill in logic (Ghidra)

Install Ghidra and run headless decompilation on the full binary. Then, for each
application function (identified by cross-referencing string addresses to their
containing functions):

1. **Locate function boundaries** — find functions that reference application
   strings (not library strings). These are our targets.
2. **Decompile each function** — Ghidra's Rust decompilation is imperfect but
   gives the branch structure, loop patterns, arithmetic, and syscall sequences.
3. **Translate to idiomatic Rust** — Ghidra outputs C-like pseudocode. Translate
   back to Rust, using the known types from Phase 1 to guide the translation.
   The skeleton's correct types make this much easier than raw decompilation.
4. **Cross-validate** — verify the decompiled logic is consistent with the debug
   messages (every log call should appear at a point that makes sense in the
   decompiled control flow).

Key areas that **cannot** be reconstructed from strings alone and require
decompilation:

- WebSocket state machine transitions (which message types are valid in which
  states, exact transition logic)
- OOM killing heuristic (memory threshold comparisons, which process to pick,
  kill-wait-check loop timing)
- Cgroup v1 vs v2 branching (exact conditional logic, fallback paths)
- Process spawning (`posix_spawnp` attribute setup, FD plumbing, signal mask)
- Local connection blocking (IP address detection, comparison logic)
- Container name validation (exact mismatch handling)
- Reattach/detach state machine (exact preconditions for each transition)
- `ProcessInfo` diagnostic output formatting
- Stdin flow control (when `ExpectStdIn` is sent, backpressure logic)
- Process group kill logic (SIGKILL to pgid, wait loop, timeout)

### Phase 3: Behavioral validation

Run both binaries under identical conditions, compare behavior.

## Verification Strategy

### Level 1: String coverage

Extract all application-level strings from both binaries and diff. Every
`[DEBUG]`, `[CONTROL]`, `[SECURITY]`, `[ERROR]`, `[INFO]`, `[OOM_KILL]` format
string in the original must appear verbatim in our reconstruction. No extra
messages should exist. This catches missing/extra code paths.

```bash
# Extract app strings from original
strings original | grep -E '^\[(DEBUG|INFO|ERROR|CONTROL|SECURITY|OOM_KILL)\]' | sort > orig_msgs.txt
# Same for reconstruction
strings reconstruction | grep -E '^\[(DEBUG|INFO|ERROR|CONTROL|SECURITY|OOM_KILL)\]' | sort > recon_msgs.txt
diff orig_msgs.txt recon_msgs.txt
```

### Level 2: Behavioral testing

The original binary is in `claude_web_env/reference/process_api.gz`. Write a
WebSocket test harness (`tests/test_protocol.rs` or Python) that:

1. Connects, sends `CreateProcess` for `echo hello`, verifies stdout +
   `ProcessExited`
2. Sends `CreateProcess` with wrong `expected_container_name`, verifies rejection
3. Sends `ProcessConnection` with `reattach=true` for a detached process
4. Sends `SendSignal` to a running process, verifies `SignalSent`
5. Spawns a memory hog, verifies `ProcessOutOfMemory` or `ContainerOutOfMemory`
6. Sends `StdInEOF`, verifies stdin closes
7. Connects from a local IP with `--block-local-connections`, verifies rejection

Run against both original and reconstruction. Diff outputs (modulo PIDs and
timestamps).

### Level 3: Ghidra cross-validation

For each function reconstructed in Phase 2, compare the control flow graph
(branch count, loop structure) against Ghidra's decompilation. Document
deviations with rationale.

### Level 4: Section size comparison

Compile reconstruction with the same rustc (1.83.0) and same crate versions.
Compare `readelf -S` section sizes. The `.text` section should be within ~20% of
the original. Large deviations indicate missing or extra logic.

## Extracted Knowledge

### Source file layout

All application source paths reference `/build/src/`, confirming the source
tree:

| File                | Purpose                                      | Evidence                                                                                         |
| ------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `main.rs`           | CLI (clap), WS listener, SIGINT, startup     | `[SECURITY] Blocking connections from local IPs`, `Listening on:`, `Failed to bind`              |
| `state.rs`          | Process map, attach/detach/reattach          | `is in an inconsistent state`, `is already attached`, `is already detached`, `Process not found` |
| `io.rs`             | WS message handler, serde structs, stdio     | `process_ws_message:`, `forward_stdin:`, `struct CreateProcess`, `struct ProcessConnection`      |
| `cgroup.rs`         | Cgroup v1/v2 setup, memory/cpu controllers   | `Cgroup v2 detected but not enabled`, `Enabled memory controller`, `cgroup.subtree_control`      |
| `oom_killer.rs`     | Container-level OOM monitoring               | `container_oom_monitor:`, `[OOM_KILL]`, `Killing process ... to free up memory`                  |
| `proc_handle.rs`    | Per-process lifecycle, wait, timeout, memory | `wait_for_child_to_exit`, `exceeded timeout`, `exceeded memory limit`, `OOM killed`              |
| `adopter.rs`        | Orphan process adoption, zombie reaping      | `monitor_orphans:`, `Reaping zombie`, `Found orphan process`, `Successfully adopted`             |
| `control_server.rs` | HTTP server for shutdown + container name    | `[CONTROL] Received shutdown request`, `Control server listening on`                             |
| `pid_tree.rs`       | `/proc/{pid}/task/{tid}/children` reader     | `/task/`, `/children` paths                                                                      |

### CLI struct (clap derive, 9 fields)

```rust
#[derive(Parser)]
struct Cli {
    #[arg(long)]
    addr: String,                              // required
    #[arg(long, default_value = "32768")]
    max_ws_buffer_size: usize,
    #[arg(long)]
    memory_limit_bytes: Option<u64>,
    #[arg(long)]
    cpu_shares: Option<u64>,
    #[arg(long, default_value = "100")]
    oom_polling_period_ms: u64,
    #[arg(long)]
    cgroupv2: bool,
    #[arg(long)]
    control_server_addr: Option<String>,
    #[arg(long)]
    block_local_connections: bool,
}
```

### CreateProcess (serde, 10 fields)

```rust
#[derive(Deserialize)]
struct CreateProcess {
    command: Vec<String>,       // inferred: process needs args
    env_vars: HashMap<String, String>,
    cwd: Option<String>,
    timeout: Option<u64>,       // seconds or ms
    clear_env: Option<bool>,
    uid: Option<u32>,
    gid: Option<u32>,
    allow_process_id_reuse: Option<bool>,
    expected_container_name: Option<String>,
    memory_limit_bytes: Option<u64>,
}
```

### ProcessConnection (serde, 3 fields)

```rust
#[derive(Deserialize)]
struct ProcessConnection {
    process_id: String,
    reattach: Option<bool>,
    create_req: Option<CreateProcess>,   // inferred from adjacent string
}
```

### Server→Client message types (22 variants)

From serde serialization strings — these are the response message type tags:

| Variant                  | When sent                                      |
| ------------------------ | ---------------------------------------------- |
| `ProcessCreated`         | After successful `CreateProcess`               |
| `AttachedToProcess`      | After successful reattach                      |
| `ProcessNotRunning`      | Reattach to non-running process                |
| `ProcessAlreadyAttached` | Reattach to already-attached process           |
| `FailedToStartProcess`   | Spawn failure                                  |
| `WithSameIdRunning`      | `allow_process_id_reuse=false` and ID conflict |
| `InfraError`             | Internal errors                                |
| `ExpectStdOut`           | Signals stdout data follows                    |
| `StdOutEOF`              | Stdout stream ended                            |
| `ExpectStdErr`           | Signals stderr data follows                    |
| `StdErrEOF`              | Stderr stream ended                            |
| `ProcessExited`          | Clean exit (with status)                       |
| `ProcessTimedOut`        | Timeout exceeded                               |
| `ProcessOutOfMemory`     | Per-process memory limit exceeded              |
| `ContainerOutOfMemory`   | Container-level memory limit exceeded          |
| `InvalidSignal`          | Bad signal number/name                         |
| `FailedToSendSignal`     | Signal delivery failed                         |
| `SignalSent`             | Signal delivered successfully                  |
| `ShuttingDown`           | Server shutting down                           |
| `SendSignal`             | Client→Server: request to send signal          |
| `ExpectStdIn`            | Flow control: ready for stdin                  |
| `StdInEOF`               | Client→Server: close stdin                     |

### Client→Server messages (inferred from handler logic)

- **First message** (text JSON): `CreateProcess` or `ProcessConnection`
- **Text messages**: `SendSignal`, `StdInEOF`, control commands
- **Binary messages**: Raw stdin data (after `ExpectStdIn`)

### Internal state types

```rust
struct ProcHandle {
    pid: u32,
    reattachable: bool,
    timeout: Option<Duration>,
    start_time: Instant,
}

struct ProcController {
    proc_handle: ProcHandle,
    controller: CgroupController,
    oom_killed_tx: Sender<()>,
    oom_killed_rx: Receiver<()>,
    stop_waiting_tx: Sender<()>,
    stop_waiting_rx: Receiver<()>,
    exit_status_tx: Sender<ExitStatus>,
    exit_status_rx: Receiver<ExitStatus>,
}

struct ProcessInfo {
    process_id: String,
    memory_limit_bytes: Option<u64>,
    memory_usage_bytes: u64,
    memory_cgroup_path: String,
    process_group_pid: u32,
    internal_state: String,
    killed_by_process_api: bool,
}
```

### Dependencies (21 crates, exact versions)

| Crate                  | Version | Role                                   |
| ---------------------- | ------- | -------------------------------------- |
| `tokio`                | 1.49.0  | Async runtime                          |
| `tokio-tungstenite`    | 0.24.0  | WebSocket server                       |
| `tungstenite`          | 0.24.0  | WebSocket protocol                     |
| `hyper`                | 1.8.1   | HTTP server (control server)           |
| `http`                 | 1.4.0   | HTTP types                             |
| `http-body-util`       | 0.1.3   | HTTP body utilities                    |
| `clap`                 | 4.5.56  | CLI argument parsing                   |
| `serde_json`           | 1.0.149 | JSON serialization                     |
| `nix`                  | 0.29.0  | Unix syscalls (signals, wait, cgroups) |
| `bytes`                | 1.11.0  | Byte buffer                            |
| `futures-core`         | 0.3.31  | Future traits                          |
| `futures-util`         | 0.3.31  | Future combinators                     |
| `futures-channel`      | 0.3.31  | Channels                               |
| `rand`                 | 0.8.5   | Random (jitter?)                       |
| `parking_lot`          | 0.12.5  | Synchronization                        |
| `once_cell`            | 1.21.3  | Lazy initialization                    |
| `smallvec`             | 1.15.1  | Small vector optimization              |
| `httpdate`             | 1.0.3   | HTTP date formatting                   |
| `httparse`             | 1.10.1  | HTTP parsing                           |
| `data-encoding`        | 2.10.0  | Base encoding                          |
| `signal-hook-registry` | 1.4.8   | Signal handling                        |
| `anstream`             | 0.6.21  | ANSI stream (from clap)                |

Plus implicit: `serde` (via `serde_json`), `clap_lex`, `clap_builder`,
`itoa`, `strsim` (from clap), `parking_lot_core`.

### Cgroup paths

| Cgroup version | Memory usage file                                         | Memory limit                            | CPU shares                                                  | Procs file                                |
| -------------- | --------------------------------------------------------- | --------------------------------------- | ----------------------------------------------------------- | ----------------------------------------- |
| v1             | `/sys/fs/cgroup/memory/process_api/memory.usage_in_bytes` | (set via `memory.limit_in_bytes`)       | `/sys/fs/cgroup/cpu/cpu.shares` or `cpu,cpuacct/cpu.shares` | `/cgroup.procs`                           |
| v2             | `/sys/fs/cgroup/process_api/memory.current`               | `/sys/fs/cgroup/process_api/memory.max` | `/sys/fs/cgroup/process_api/cpu.weight`                     | `/sys/fs/cgroup/process_api/cgroup.procs` |

### Version stamp convention

Each source file includes a header comment identifying the target binary:

```rust
//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
```

The Bazel target and `Cargo.toml` `[package]` section also embed the release
tag:

```toml
[package]
name = "process_api_re"
version = "0.1.0"  # matches original package version
```

```python
# BUILD.bazel
rust_binary(
    name = "process_api_re",
    ...
)
```

If the reference binary is updated, create a new directory
`re/process_api_YYYY-MM-DD/` (matching the release date stamp) or update the
existing source with a new header comment. The `PLAN.md` table at the top is the
canonical record of which binary version is being reconstructed.

## Build

The reconstruction targets Bazel (matching the repo's build system). New
dependencies are added to the root `Cargo.toml` and pinned via
`CARGO_BAZEL_REPIN=1 bazel build @crates//:all`.

```bash
bazel build //re/process_api:process_api_re
bazel build --config=rust-check //re/process_api:all
```

## File listing

```
re/process_api/
├── PLAN.md              # This file
├── BUILD.bazel          # Bazel build definition
└── src/
    ├── main.rs          # CLI, WS listener, signal handling, startup
    ├── state.rs         # Process map, attach/detach state machine
    ├── io.rs            # WS protocol, serde structs, stdin/stdout/stderr
    ├── cgroup.rs        # Cgroup v1/v2 management
    ├── oom_killer.rs    # Container-level OOM monitor
    ├── proc_handle.rs   # Per-process lifecycle (wait, timeout, memory)
    ├── adopter.rs       # Orphan adoption, zombie reaping
    ├── control_server.rs # HTTP control server
    └── pid_tree.rs      # /proc PID tree reader
```

## Confidence levels

**Types/structs**: High confidence from strings alone. Field names, counts, and
enum variants are embedded verbatim by serde/clap codegen.

**Logic**: Requires decompilation. Strings tell us _where_ branches exist (each
log message is a branch point) but not the _conditions_. Confidence levels below
assume successful Ghidra decompilation.

| Module              | Types confidence | Logic confidence | Key unknowns requiring decompilation                                                |
| ------------------- | ---------------- | ---------------- | ----------------------------------------------------------------------------------- |
| `main.rs`           | **High**         | **Medium**       | Signal mask setup, listener accept loop structure, connection blocking IP detection |
| `state.rs`          | **High**         | **Medium**       | Exact preconditions for attach/detach/reattach transitions, lock ordering           |
| `io.rs`             | **High**         | **Medium-Low**   | WS state machine (which messages valid when), stdin backpressure, spawn FD plumbing |
| `cgroup.rs`         | **High**         | **Medium**       | v1/v2 detection logic, controller enablement sequence, permission handling          |
| `oom_killer.rs`     | **Medium**       | **Low**          | Threshold math, process selection heuristic, kill-wait-retry loop timing            |
| `proc_handle.rs`    | **High**         | **Medium**       | `posix_spawnp` attribute setup, process group kill sequence, timeout calculation    |
| `adopter.rs`        | **High**         | **Medium**       | Orphan scan interval, zombie tracking data structure, adoption criteria             |
| `control_server.rs` | **High**         | **High**         | Simple HTTP handler — decompilation should be straightforward                       |
| `pid_tree.rs`       | **High**         | **High**         | Reads `/proc` files — trivial parsing logic                                         |

## Status

- [ ] Bazel build setup (BUILD.bazel, deps in Cargo.toml)
- [ ] Phase 1: Type skeleton from strings
  - [ ] `main.rs` — CLI struct, module declarations, tokio entrypoint
  - [ ] `state.rs` — ProcessMap, state types, method signatures
  - [ ] `io.rs` — CreateProcess, ProcessConnection, message enum, fn signatures
  - [ ] `cgroup.rs` — CgroupVersion enum, controller types, fn signatures
  - [ ] `oom_killer.rs` — monitor task signature, ProcessInfo
  - [ ] `proc_handle.rs` — ProcHandle, ProcController, fn signatures
  - [ ] `adopter.rs` — orphan monitor signature
  - [ ] `control_server.rs` — HTTP handler signatures
  - [ ] `pid_tree.rs` — get_children signature
- [ ] Skeleton compiles (with `todo!()` bodies)
- [ ] Phase 2: Ghidra decompilation → fill in logic
  - [ ] Install Ghidra headless
  - [ ] Identify application functions (by string xrefs)
  - [ ] Decompile and translate `pid_tree.rs` (easiest, validates workflow)
  - [ ] Decompile and translate `control_server.rs`
  - [ ] Decompile and translate `cgroup.rs`
  - [ ] Decompile and translate `state.rs`
  - [ ] Decompile and translate `adopter.rs`
  - [ ] Decompile and translate `proc_handle.rs`
  - [ ] Decompile and translate `oom_killer.rs`
  - [ ] Decompile and translate `io.rs` (hardest — largest, most state)
  - [ ] Decompile and translate `main.rs`
- [ ] Full build compiles (no `todo!()` remaining)
- [ ] Phase 3: Verification
  - [ ] String coverage diff passes
  - [ ] Behavioral test harness written
  - [ ] Behavioral tests pass against original binary
  - [ ] Behavioral tests pass against reconstruction
  - [ ] Section size comparison within tolerance
