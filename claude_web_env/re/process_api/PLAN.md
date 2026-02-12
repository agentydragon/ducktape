# process_api Reverse Engineering Plan

## Target Binary

| Property                  | Value                                             |
| ------------------------- | ------------------------------------------------- |
| **Release**               | `process_api_2026-02-02-04-57`                    |
| **Package version**       | `0.1.0`                                           |
| **ELF Build ID**          | `b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0`        |
| **SHA256 (decompressed)** | `066a048964945600...`                             |
| **SHA256 (.gz)**          | `10351f86963fb350...`                             |
| **Reference file**        | `claude_web_env/reference/process_api.gz`         |
| **Language**              | Rust                                              |
| **Compiler**              | `rustc 1.83.0 (90b35a623 2024-11-26)`             |
| **Linking**               | Dynamic (libc, libm, libgcc_s)                    |
| **Stripped**              | Yes (no debug info, no symbol table)              |
| **`.text` range**         | `0x1d060` – `0x1a8d0c` (1,621,164 bytes, ~1.6 MB) |
| **Total binary size**     | 2,087,504 bytes (~2.0 MB)                         |

## Goal

Produce compilable Rust source code that is **semantically equivalent** to the
original binary: same WebSocket protocol, same process management behavior, same
cgroup logic, same error handling paths. Not byte-identical — the goal is
readable, accurate source that could replace the binary.

Each reconstructed function references the binary address range it was
decompiled from, so the reconstruction is auditable against the original.

## Approach: Decompilation-First

### Step 1: Full Ghidra decompilation

Run Ghidra headless analysis on the complete binary. This produces a decompiled
C-like pseudocode database covering the entire `.text` section.

### Step 2: Identify application functions via string cross-references

The binary has ~200 unique application-level strings (`[DEBUG]`, `[CONTROL]`,
etc.) with known source file attributions (`/build/src/*.rs` panic paths). For
each string:

1. Find its address in `.rodata`
2. Find all functions that reference that address (Ghidra xrefs)
3. Map those functions to their source file based on which strings they reference

This produces a **function catalog**: a mapping of `(binary address range) →
(source file, purpose)` for every application function. Library functions
(tokio, hyper, serde, etc.) are identified by their crate path strings and
excluded from reconstruction.

### Step 3: Decompile each function, translate to Rust

For each application function in the catalog:

1. Read Ghidra's decompiled pseudocode
2. Translate to idiomatic Rust, using known types (serde field names, clap
   struct, message enums) to guide the translation
3. Annotate each function with its binary address range:

   ```rust
   /// Decompiled from 0x4a120..0x4a3f0
   /// Xrefs: "[DEBUG] Starting orphan monitor task"
   async fn monitor_orphans(state: Arc<Mutex<ProcessMap>>, shutdown: ...) {
       // ...
   }
   ```

4. Validate: every debug/error string referenced by the function at its binary
   address must appear in the reconstructed Rust at the corresponding branch

### Step 4: Assemble into compilable source

Combine the translated functions into the known module structure (9 source
files). Wire up cross-module calls. Build with Bazel.

### Step 5: Behavioral validation

Run both binaries with identical inputs, compare outputs.

## Tooling

- **Ghidra** (headless mode) — full binary decompilation. Install via
  `ghidra_11.3.2_PUBLIC` or similar.
- **objdump/readelf** — section layout, PLT entries, relocation info
- **strings + custom scripts** — string→address mapping, function catalog
  generation
- **Bazel + rules_rust** — build the reconstruction

## Verification Strategy

### Level 1: String coverage

Extract all application-level strings from both binaries and diff. Every
`[DEBUG]`, `[CONTROL]`, `[SECURITY]`, `[ERROR]`, `[INFO]`, `[OOM_KILL]` format
string in the original must appear verbatim in our reconstruction.

### Level 2: Behavioral testing

Write a WebSocket test harness that exercises the protocol against both the
original and reconstruction:

1. `CreateProcess` → stdout + `ProcessExited`
2. Wrong `expected_container_name` → rejection
3. `ProcessConnection` reattach to detached process
4. `SendSignal` to running process → `SignalSent`
5. Memory hog → `ProcessOutOfMemory` / `ContainerOutOfMemory`
6. `StdInEOF` → stdin closes
7. Local IP with `--block-local-connections` → rejected

### Level 3: Address-level traceability

Every function in the reconstruction has a `/// Decompiled from 0xAAAA..0xBBBB`
comment. This lets anyone verify any function by opening the binary at that
address in Ghidra and comparing the logic.

### Level 4: Section size comparison

Compile with the same rustc (1.83.0) and crate versions. `.text` section size
should be within ~20% of the original.

## Extracted Knowledge (from strings analysis)

### Source file layout

All application source paths reference `/build/src/`:

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
    command: Vec<String>,
    env_vars: HashMap<String, String>,
    cwd: Option<String>,
    timeout: Option<u64>,
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
    create_req: Option<CreateProcess>,
}
```

### Server→Client message types (22 variants)

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

Each source file includes a header comment referencing the target binary and the
address range(s) it was decompiled from:

```rust
//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Functions decompiled from:
//!   monitor_orphans: 0xAAAAA..0xBBBBB
//!   adopt_orphan:    0xCCCCC..0xDDDDD
```

The `PLAN.md` table at the top is the canonical record of which binary version
is being reconstructed.

## Build

Targets Bazel (matching the repo's build system). New dependencies are added to
the root `Cargo.toml` and pinned via
`CARGO_BAZEL_REPIN=1 bazel build @crates//:all`.

```bash
bazel build //claude_web_env/re/process_api:process_api_re
bazel build --config=rust-check //claude_web_env/re/process_api:all
```

## File listing

```
claude_web_env/re/process_api/
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

## Status

- [ ] Install Ghidra headless + run full binary analysis
- [ ] Generate function catalog (address → source file mapping via string xrefs)
- [ ] Bazel build setup (BUILD.bazel, deps in Cargo.toml)
- [ ] Decompile and translate each module (ordered by difficulty):
  - [ ] `pid_tree.rs` — trivial `/proc` reader, validates workflow
  - [ ] `control_server.rs` — simple HTTP handler
  - [ ] `cgroup.rs` — filesystem operations, v1/v2 branching
  - [ ] `state.rs` — process map state machine
  - [ ] `adopter.rs` — orphan scanner, zombie reaper
  - [ ] `proc_handle.rs` — process lifecycle, spawn, wait
  - [ ] `oom_killer.rs` — container OOM monitor
  - [ ] `main.rs` — CLI, listener, signal handling, wiring
  - [ ] `io.rs` — WebSocket protocol handler (largest, most complex)
- [ ] Full build compiles
- [ ] Verification:
  - [ ] String coverage diff passes
  - [ ] Behavioral test harness written
  - [ ] Behavioral tests pass against both binaries
  - [ ] Every function has `Decompiled from 0x...` annotation
