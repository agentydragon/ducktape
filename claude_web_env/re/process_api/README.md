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

Every function is annotated with `/// Decompiled from 0xAAAA..0xBBBB` and
`/// Xrefs:` referencing the binary address range and string cross-references,
so the reconstruction is auditable against the original.

## Extracted Protocol

Clients connect via WebSocket and send a JSON text message as the first frame:
either a `CreateProcess` (spawn a new process) or a `ProcessConnection`
(reattach to a detached process). Server responds with tagged JSON messages
(`{"type": "ProcessCreated", ...}`). Stdout/stderr are sent as
`ExpectStdOut`/`ExpectStdErr` text frames followed by binary data frames.
Stdin uses `ExpectStdIn` + binary frame.

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
