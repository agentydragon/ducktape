# process_api Reverse Engineering

Reverse-engineered source code for `process_api`, Anthropic's container init
process (PID 1) for Claude Code web containers. The binary manages process
lifecycles over WebSocket, handles cgroup-based resource limits, and performs
PID 1 duties (orphan adoption, zombie reaping).

## Target Binary

| Property           | Value                                                               |
| ------------------ | ------------------------------------------------------------------- |
| **ELF Build ID**   | `edebff2c28de76238c95c299ba3401a9098c9e17`                          |
| **Release**        | `process_api_2026-05-11-18-55`                                      |
| **MD5**            | `78f08d09b8b626ef1d48904161b27739`                                  |
| **SHA-256**        | `06e438d1757ad998978d1592884019d6922daf5a7c1d52f5b537377c97cbf89b`  |
| **Reference file** | `devinfra/claude/web_env/reference/process_api.gz`                  |
| **Language**       | Rust                                                                |
| **Stripped**       | Yes (no debug info, no symbol table)                                |
| **Linking**        | Static-pie                                                          |
| **Binary size**    | 4,377,896 bytes uncompressed (`.text` 3,547,304, `.rodata` 405,644) |
| **Rust toolchain** | `rustc 1.95.0-nightly (6a979b3e3 2026-02-26)`                       |
| **Source paths**   | Remapped: application modules appear as bare `src/*.rs`             |

Reconstructed source lives under `src/` in this directory.

## Build

```bash
bazel build //devinfra/claude/web_env/re/process_api:process_api_re
```

## Approach

String-anchored decompilation. The binary is stripped, so every claim traces to
one of four kinds of evidence:

1. **Panic-location tables.** `core::panic::Location` records in `.data.rel.ro`
   pair a source-file string with a line and column. They reveal the module
   list (`src/*.rs`) and pin individual `expect`/`assert` sites to line numbers.
2. **Interned string runs in `.rodata`.** rustc concatenates literals without
   separators; serde variant tags, field names and log templates appear in
   source order, so a run is itself structural evidence.
3. **Format templates.** rustc 1.95 packs `format_args!` into a byte template
   (length-prefixed literal chunks, `0xc0` placeholder markers, `0x00`
   terminator). Reading the template recovers the exact message and its
   argument count.
4. **Disassembly** (`objdump -d`) anchored on `.rodata` addresses: find the LEA
   that loads a string, then read outward. Function boundaries come from the
   set of `call` targets.

Serde `FIELDS` arrays are read directly out of `.data.rel.ro` by resolving
`R_X86_64_RELATIVE` addends and the adjacent length words — that gives struct
field names in declaration order.

Every recovered function is annotated with `/// Decompiled from 0xAAAA..0xBBBB`
and `/// Xrefs:`, so the reconstruction is auditable against the original.
Anything not actually read is marked `TODO(re):`, `GUESS:` or `STUB:` in the
source.

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

### Source Files

The binary's own module list, from the `src/*.rs` strings in its
panic-location table:

| Module                 | Purpose                                                 | Recovered |
| ---------------------- | ------------------------------------------------------- | --------- |
| `main.rs`              | CLI, cgroup init, WS/vsock/dial-uds listeners, shutdown | yes       |
| `io.rs`                | WebSocket protocol, process I/O, JWT auth               | yes       |
| `state.rs`             | Process map state machine                               | yes       |
| `proc_handle.rs`       | Per-process lifecycle, wall-clock + CPU timeout, kill   | yes       |
| `cgroup.rs`            | Cgroup v1/v2 setup, memory/CPU                          | yes       |
| `control_server.rs`    | HTTP control API (TCP + vsock)                          | yes       |
| `oom_killer.rs`        | Container + per-process OOM monitors                    | yes       |
| `adopter.rs`           | Orphan adoption, zombie reaping                         | yes       |
| `pid_tree.rs`          | `/proc` PID tree traversal                              | yes       |
| `firecracker_init.rs`  | Firecracker VM init, egress-CA fan-out                  | partial   |
| `platform/unix/mod.rs` | Platform-specific vsock/UDS abstractions                | yes       |
| `ws_compression.rs`    | zstd stream encode/decode for WebSocket payloads        | partial   |
| `trace.rs`             | Trace-event emission (`##TRACE##` marker)               | no        |

`trace.rs` is present in the binary (panic locations at `.data.rel.ro`
0x4211f0/0x421208/0x421220, marker string `##TRACE##` at 0x4211e0) but has no
counterpart under `src/` yet.

### Function Address Map

Addresses established against the current binary (`edebff2c`) by string
cross-reference plus call-target boundaries. Anything not listed here still
carries a stale address in the source doc comments — see <PLAN.md>.

| Function                         | Address range        | Module                |
| -------------------------------- | -------------------- | --------------------- |
| `handle_ws` (connection owner)   | `0x1428f0..0x1492d0` | `io.rs`               |
| `process_ws_message`             | `0x14bc40..0x153610` | `io.rs`               |
| stderr `pipe_to_ws`              | `0x4a1f0..0x4bb60`   | `io.rs`               |
| stdout `pipe_to_ws`              | `0x4c310..0x4dc80`   | `io.rs`               |
| `ProcessConnection` deserializer | `0x15cc30..0x15e400` | `io.rs`               |
| `CreateProcess` deserializer     | `0x186c00..0x188e70` | `io.rs`               |
| `ServerMessage` JSON encoder     | `0x1856e0..0x185d00` | `io.rs`               |
| JSON string escaper              | `0x1850c0..0x185440` | `io.rs`               |
| `wait_for_child_to_exit`         | `0x58f00..0x5a600`   | `proc_handle.rs`      |
| graceful-shutdown driver         | `0x154810..0x158a80` | `main.rs`             |
| zstd `StreamEncoder::new`        | `0x11ff20..0x120200` | `ws_compression.rs`   |
| zstd stream encode               | `0x1bd5c0..0x1bf330` | `ws_compression.rs`   |
| `append_ca_cert` (orchestrator)  | `0xfab40..0xfc530`   | `firecracker_init.rs` |
| PEM splitter                     | `0x101090..0x101570` | `firecracker_init.rs` |
| Java JKS injector                | `0xfc530..0xfe330`   | `firecracker_init.rs` |
| NSS DB injector                  | `0xfe330..0x100770`  | `firecracker_init.rs` |
| Chromium policy writer           | `0x100770..0x101090` | `firecracker_init.rs` |
| Python bundle patcher            | `0xf5f90..0xf7630`   | `firecracker_init.rs` |
| gcloud bundle patcher            | `0xf37d0..0xf5cd0`   | `firecracker_init.rs` |
| npmrc writer                     | `0xf8d60..0xf9910`   | `firecracker_init.rs` |
| pip.conf writer                  | `0xf9910..0xfa570`   | `firecracker_init.rs` |
| uv.toml writer                   | `0xfa570..0xfab40`   | `firecracker_init.rs` |
| sudoers `env_keep` writer        | `0xf8910..0xf8d60`   | `firecracker_init.rs` |
| chown helper                     | `0xf5cd0..0xf5f90`   | `firecracker_init.rs` |

## CLI Arguments

```
process_api [OPTIONS]

Options:
  --addr <ADDR>                    WebSocket listen address (e.g., "0.0.0.0:2024")
  --max-ws-buffer-size <SIZE>      WebSocket frame buffer size [default: 32768]
  --memory-limit-bytes <BYTES>     Container-level memory limit (enables OOM monitor)
  --cpu-shares <SHARES>            CPU weight — cgroup v1 cpu.shares or v2 cpu.weight
  --oom-polling-period-ms <MS>     OOM check interval [default: 100]
  --cgroupv2                       Force cgroup v2 mode (auto-detected otherwise)
  --control-server-addr <ADDR>     HTTP control server (e.g., "0.0.0.0:2025")
                                   When set, SIGINT handler is disabled
  --block-local-connections        Reject 127.0.0.1, ::1, 0.0.0.0, :: on both servers
  --listen-uds <PATH>             Listen on a Unix domain socket instead of TCP
  --dial-uds <PATH>               Dial out to host-side UDS bridge (gVisor)
  --listen-vsock-port <PORT>      Listen on vsock port for WebSocket (Firecracker)
  --control-vsock-port <PORT>     Control server on vsock port (Firecracker)
  --firecracker-init               Run as Firecracker VM init
```

All flags accept corresponding `SCREAMING_SNAKE_CASE` environment variables
(e.g., `MEMORY_LIMIT_BYTES`, `CONTROL_SERVER_ADDR`, `FIRECRACKER_INIT`).

### `--firecracker-init` Mode

When `--firecracker-init` is set, `process_api` runs a full VM init sequence
before starting the WebSocket listener:

1. Mount root partition (`/dev/vda`)
2. `pivot_root` to mounted filesystem
3. Set up networking (socket creation, interface configuration)
4. Set up FUSE (`/dev/fuse`, FUSE service URL)
5. Mount rclone_tools (remote storage); the rclone VFS cache lives at
   `/dev/shm/rclone-vfscache` (exported as `RCLONE_CACHE_DIR`)
6. Parse `container.env` JSON for memory and filestore mount config
7. Mount memory and filestore destinations
8. Install the egress CA (see below)
9. Spawn the main process

### Egress CA Injection

When the mount config carries `ca_cert_pem` (or `POST
/auth_public_key/write_etc_files` carries `ca_cert`), `process_api` installs
that PEM into every trust store the sandbox's toolchains consult, then exports
the matching environment variables so all children of PID 1 inherit them.

| Target                   | What is written                                                                                                                                                                                                               |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| System anchors           | `/usr/local/share/ca-certificates/sandboxing-egress-ca.crt`, `/etc/ssl/certs/sandboxing-egress-ca.pem`                                                                                                                        |
| Merged bundles           | `/etc/ssl/certs/ca-certificates.crt` plus `etc/pki/tls/{certs/ca-bundle.crt,cacert.pem}`, `etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`, `etc/ssl/ca-bundle.pem`, `var/lib/ca-certificates/ca-bundle.pem`                |
| dpkg bookkeeping         | appends the anchor to `/var/lib/dpkg/info/ca-certificates.list` so `update-ca-certificates` keeps it                                                                                                                          |
| Python                   | `certifi/cacert.pem`, `pip/_vendor/certifi/cacert.pem`, `botocore/cacert.pem` under every discovered `site-packages` / `dist-packages`, plus `/opt/conda/ssl/cacert.pem`                                                      |
| google-cloud-sdk         | the four vendored `certifi`/`botocore` bundles under each SDK root                                                                                                                                                            |
| pip                      | `[global]` `cert = <path>`                                                                                                                                                                                                    |
| npm                      | `cafile=<path>` in `etc/npmrc`, `usr/etc/npmrc`, `usr/local/etc/npmrc`                                                                                                                                                        |
| uv                       | `native-tls = true` in `/etc/uv/uv.toml` (only if a `uv` binary exists)                                                                                                                                                       |
| Java                     | `keytool -importcert -storepass changeit -alias sandboxing-egress-ca-<n>` into every `cacerts` found                                                                                                                          |
| NSS                      | `certutil -N --empty-password` then `certutil -A -t C,,` against every `nssdb`                                                                                                                                                |
| Firefox                  | `policies.json` with `policies.Certificates.Install`                                                                                                                                                                          |
| Chromium / Chrome / Edge | `sandboxing-ca.json` in each managed-policy directory                                                                                                                                                                         |
| Environment              | `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `PIP_CERT`, `CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE`, `HTTPLIB2_CA_CERTS`, `GIT_SSL_CAINFO`, `AWS_CA_BUNDLE`, `SSL_CERT_DIR`, `NIX_SSL_CERT_FILE` |
| sudo                     | `/etc/sudoers.d/90-sandbox-ca-env` with `Defaults env_keep +=` for those vars plus the proxy vars                                                                                                                             |

Every step is best-effort: failures log `[INIT] WARNING: ...` and boot
continues. If the whole helper fails during Firecracker init, a marker file
`<root>/.sandboxing-ca-inject-failed` records the reason.

This mode is used in the current live container invocation:

```
/process_api --firecracker-init --addr 0.0.0.0:2024 --max-ws-buffer-size 32768 --block-local-connections
```

## WebSocket Protocol

Clients connect via WebSocket and send a JWT token as the first text message
for authentication. The server verifies it using an Ed25519 public key loaded
via `POST /auth_public_key/write_etc_files`. If no auth public key is loaded,
JWT is accepted without verification. After JWT authentication, the client
sends a JSON text message: either a `CreateProcess` (spawn a new process) or
a `ProcessConnection` (reattach to a detached process). Server responds with tagged JSON messages
(`{"type": "ProcessCreated", ...}`). Stdout/stderr are sent as
`ExpectStdOut`/`ExpectStdErr` text frames followed by binary data frames.
Stdin uses `ExpectStdIn` + binary frame.

### First Message: JWT Token

The client sends a JWT token as a text WebSocket frame. The server verifies
the token signature using the Ed25519 public key (if loaded). Key strings:
`[DEBUG] Received JWT token, verifying...`,
`[DEBUG] JWT verified successfully: sub='...`,
`Invalid JWT signature`, `JWT token has expired`,
`JWT authentication failed: `, `JWT decode error: `,
`JWT key error: `, `Missing required claim: `,
`[DEBUG] No auth public key loaded, accepting JWT without verification`,
`Client closed connection after JWT`,
`Second message after JWT should be text json CreateProcess`.

Uses `jsonwebtoken 9.3.1` crate with `TokenClaims` (3 fields) and
`ClaimsForValidation` (5 fields) structs.

### Second Message: `CreateProcess`

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
  "cpu_timeout": 120,
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
| `timeout`                | `u64?`              | —       | Kill after N wall-clock seconds              |
| `cpu_timeout`            | `u64?`              | —       | Kill after N seconds of cgroup CPU time      |
| `memory_limit_bytes`     | `u64?`              | —       | Per-process memory limit via cgroup          |

Evidence: `struct CreateProcess with 11 elements` (0x39a02b); the `cpu_timeout`
field-name compare is at 0x186fa5.

The spawned process runs in a new session (`setsid`), with piped
stdin/stdout/stderr. If `memory_limit_bytes` is set, a per-process cgroup is
created under `/sys/fs/cgroup/process_api/{pid}/`.

### Second Message: `ProcessConnection`

Reattach to a previously detached process, or query its state.

```json
{
  "process_id": "/bin/bash",
  "reattach": true,
  "expected_container_name": "my-container",
  "want_trace_events": true,
  "accept_zstd": true
}
```

| Field                     | Type      | Default | Description                            |
| ------------------------- | --------- | ------- | -------------------------------------- |
| `process_id`              | `string`  | —       | ID of process to reconnect to          |
| `reattach`                | `bool?`   | `true`  | Actually reattach (false = just query) |
| `expected_container_name` | `string?` | —       | Validate container identity            |
| `want_trace_events`       | `bool?`   | `false` | Request the `TraceEvent` stream        |
| `accept_zstd`             | `bool?`   | `false` | Client can decode zstd binary frames   |

Evidence: `struct ProcessConnection with 5 elements` (0x39a074); the field-name
literals are loaded at 0x15ddd5..0x15de0f.

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
| `KeepAlive`   | —                | WebSocket keepalive                              |

Supported signals: `SIGHUP`, `SIGINT`, `SIGQUIT`, `SIGKILL`, `SIGTERM`,
`SIGUSR1`, `SIGUSR2`, `SIGCONT`, `SIGSTOP`. Numeric values also accepted.

### Server-to-Client Messages

All responses are tagged JSON text messages (`{"type": "...", ...}`):

| Message                  | Fields                              | Description                                |
| ------------------------ | ----------------------------------- | ------------------------------------------ |
| `ProcessCreated`         | `process_id`, `pid`                 | Process spawned successfully               |
| `AttachedToProcess`      | `process_id`, `pid`                 | Reattached to detached process             |
| `ProcessCreatedV2`       | `process_id`, `pid`                 | Extended form of `ProcessCreated`          |
| `AttachedToProcessV2`    | `process_id`, `pid`, `capabilities` | Reattached with capability negotiation     |
| `ProcessNotRunning`      | `process_id`                        | Process not found or already exited        |
| `ProcessAlreadyAttached` | `process_id`                        | Another WS is attached to this process     |
| `FailedToStartProcess`   | `error`                             | Spawn failed                               |
| `WithSameIdRunning`      | `process_id`                        | Duplicate ID (and reuse disallowed)        |
| `InfraError`             | `error`                             | Infrastructure error (name mismatch)       |
| `ExpectStdOut`           | —                                   | Next binary frame is stdout data           |
| `StdOutEOF`              | —                                   | Stdout pipe closed                         |
| `ExpectStdErr`           | —                                   | Next binary frame is stderr data           |
| `StdErrEOF`              | —                                   | Stderr pipe closed                         |
| `ProcessExited`          | `status: i32`, `details: string`    | Normal exit or signal death                |
| `ProcessTimedOut`        | `timeout_secs`, `details`           | Killed after the wall-clock timeout        |
| `ProcessCpuTimedOut`     | `cpu_timeout_secs`, `details`       | Killed after the cgroup CPU-time budget    |
| `ProcessOutOfMemory`     | `limit_bytes`, `details`            | Per-process memory limit exceeded          |
| `ContainerOutOfMemory`   | `limit_bytes`, `details`            | Container-level OOM kill                   |
| `TraceEvent`             | `TraceEventMsg` fields              | Trace event; sent when `want_trace_events` |
| `InvalidSignal`          | `signal`                            | Unrecognized signal name/number            |
| `FailedToSendSignal`     | `error`                             | Signal delivery failed                     |
| `SignalSent`             | `signal`                            | Signal delivered successfully              |
| `KeepAlive`              | —                                   | WebSocket keepalive                        |
| `Closed`                 | —                                   | Connection closed                          |
| `AlreadyClosed`          | —                                   | Connection already closed                  |
| `IoWriteBufferFull`      | —                                   | I/O write buffer full                      |
| `AttackAttemptUrl`       | —                                   | Rejected URL attack attempt                |
| `HttpFormatIpSocket`     | —                                   | HTTP-formatted IP socket info              |
| `ShuttingDown`           | —                                   | Server is shutting down                    |

#### `ConnectionCapabilities`

Sent as part of `AttachedToProcessV2`. Evidence: the interned run
`ConnectionCapabilities` / `supports_trace` / `supports_zstd` at 0x39a8b3, and
the JSON serializer at 0x185440..0x1854ff which emits both keys.

```json
{ "supports_trace": true, "supports_zstd": true }
```

#### `TraceEventMsg`

5-element serde struct. Evidence: `struct TraceEventMsg with 5 elements`.
Fields from the serde field-name run at 0x399f8a: `process`, `host`, `sph`,
`cat`, `dur_us`.

Sent as `TraceEvent` WS messages when `want_trace_events=true` in
`ProcessConnection`.

> **Note**: The `process_id` field in `ProcessConnection` cannot contain the
> string `##TRACE##` (validated at server side). This string is used as an
> internal trace marker.

### I/O Forwarding Sequence

```
Server                              Client
  │                                   │
  │◄── JWT token (text) ─────────────│
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

### Payload Compression (zstd)

When the client sets `accept_zstd` in `ProcessConnection`, the server answers
`ConnectionCapabilities { supports_zstd: true }` and builds one streaming zstd
encoder per output stream, and a decoder for inbound binary frames.

Parameters read out of the binary:

| Parameter                       | Value                | Evidence                   |
| ------------------------------- | -------------------- | -------------------------- |
| `ZSTD_c_compressionLevel`       | 3                    | 0x11ff40 (`$0x64`, `$0x3`) |
| `ZSTD_c_windowLog`              | 15 (32 KiB)          | 0x11ffa2 (`$0x65`, `$0xf`) |
| `ZSTD_d_windowLogMax`           | 15                   | 0x14bf5b (`$0x64`, `$0xf`) |
| Stream scratch buffer           | 32 KiB (`0x8000`)    | 0x11ff8b, 0x14bf44         |
| Max decompressed size per frame | 64 MiB (`0x4000000`) | 0x14bfc2                   |

Exceeding the decompression cap produces
`decompressed output exceeds 67108864 bytes`.

The zstd C library is statically linked (`zstd-safe 7.2.4` on the Rust side);
its error-string table lives at `.rodata` 0x3abe08..0x3ac360.

## HTTP Control Server

When `--control-server-addr` is set, the SIGINT handler is disabled and
shutdown is driven exclusively through HTTP.

| Method | Path                               | Request body      | Response                               |
| ------ | ---------------------------------- | ----------------- | -------------------------------------- |
| `POST` | `/shutdown`                        | —                 | `200 "Shutdown initiated\n"`           |
| `POST` | `/container_name`                  | UTF-8 name string | `200 "Container name set to: X\n"`     |
| `POST` | `/auth_public_key/write_etc_files` | JSON body         | `200`/`400`/`500` (key, etc files, CA) |
| `POST` | `/mount_root`                      | JSON config       | `200` or `500` (Firecracker snapstart) |
| `POST` | `/fs_freeze`                       | —                 | `200` (FIFREEZE filesystem)            |
| `POST` | `/fs_thaw`                         | —                 | `200` (FITHAW filesystem)              |
| `POST` | `/sync_clock`                      | JSON/integer      | `200` clock synced (`clock_settime`)   |
| `GET`  | `/health`                          | —                 | `200` diagnostic text                  |
| `GET`  | `/container_name`                  | —                 | `200 "X\n"` or `"not set\n"`           |
| `*`    | `*`                                | —                 | `404 "Not Found\n"`                    |

**`POST /shutdown`** performs `sync(1)` before sending the broadcast shutdown
signal. All tracked processes are then killed. The shutdown driver then waits a
one-second grace period; any tasks still alive produce
`[WARN] N task(s) still alive after 1s shutdown grace, aborting` on stderr.

**`POST /auth_public_key/write_etc_files`** accepts an `EtcFiles` body with
three fields — `process`, `hosts` and `ca_cert` (evidence: `struct EtcFiles
with 3 elements` at 0x39a00c). `ca_cert` is fanned out through the same helper
the Firecracker init path uses; failure returns `500` with body
`append_ca_cert: <err>`. On success the server logs
`[CONTROL] /write_etc_files: hosts N bytes, resolv N bytes, ca_cert ...`.

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

### CPU-Time Enforcement

A process created with `cpu_timeout` is additionally checked against its
cgroup's cumulative CPU usage. `wait_for_child_to_exit` reads
`<cgroup>/cpu.stat` and parses the `usage_usec ` line; once that exceeds the
budget the process tree is killed and `ProcessCpuTimedOut` is sent.

If the cgroup has no readable `cpu.stat`, the feature degrades:

```text
[DEBUG] Process X (PID N) cpu.stat unavailable (E); cpu_timeout not enforced, falling back to wall-clock timeout only
```

A handle with no cgroup at all fails earlier with `no cgroup for this process`.

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
   - Wall-clock timeout expiry (`timeout`)
   - CPU-time budget expiry (`cpu_timeout`)
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
3. Log version: `[INFO] process_api release: process_api_2026-05-11-18-55`
4. Set up cgroup hierarchy (with retry loop on failure, 10s backoff)
5. Set CPU shares if configured
6. Detect container name from `/container_info.json` (if present)
7. Start control server OR SIGINT handler (mutually exclusive)
8. Start orphan monitor task
9. Start container OOM monitor task (if memory limit set)
10. Bind WebSocket listener, enter accept loop
11. On shutdown signal: kill all tracked processes, wait a 1-second grace period
    for outstanding tasks, log completion

## Container Integration

In the live Claude Code web container, `process_api` is PID 1 and is invoked as:

```text
/process_api --firecracker-init \
  --addr 0.0.0.0:2024 \
  --max-ws-buffer-size 32768 \
  --block-local-connections \
  --listen-vsock-port 2024
```

`environment-manager` (the next binary in the boot chain) connects via
WebSocket to port 2024 to spawn the Claude Code agent process. The
orchestration layer connects to port 2025 for lifecycle management.

See <../../docs/environment_discovery.md> for the full container boot sequence
and how `process_api` fits into the `process_api → environment-manager →
claude` process tree.

## Dependencies

Versions come from the crate source paths embedded in the binary's
panic-location table (`/root/.cargo/registry/src/artifactory.infra.ant.dev-*/<crate>-<version>/`).

| Crate                  | Version | Purpose                                  |
| ---------------------- | ------- | ---------------------------------------- |
| `tokio`                | 1.52.2  | Async runtime                            |
| `tokio-tungstenite`    | —       | WebSocket server                         |
| `hyper`                | 1.9.0   | HTTP/1.1 control server                  |
| `http`                 | 1.4.0   | HTTP types                               |
| `httparse`             | 1.10.1  | HTTP request parsing                     |
| `httpdate`             | 1.0.3   | HTTP date formatting                     |
| `http-body-util`       | 0.1.3   | HTTP body handling                       |
| `serde` + `serde_json` | —       | JSON serialization/deserialization       |
| `clap`                 | —       | CLI argument parsing                     |
| `nix`                  | 0.29.0  | Unix syscalls (signals, waitpid, setsid) |
| `parking_lot`          | 0.12.5  | Synchronous mutex (for process map)      |
| `futures-channel`      | 0.3.32  | Stream/Sink plumbing                     |
| `bytes`                | 1.11.1  | Byte buffer utilities                    |
| `itoa`                 | 1.0.18  | Integer formatting                       |
| `once_cell`            | 1.21.4  | Lazy statics                             |
| `smallvec`             | 1.15.1  | Small-vector optimization                |
| `mio`                  | 1.2.0   | Non-blocking IO (tokio backend)          |
| `jsonwebtoken`         | 9.3.1   | JWT authentication (Ed25519 verify)      |
| `tokio-vsock`          | —       | AF_VSOCK socket support (Firecracker)    |
| `zstd-safe`            | 7.2.4   | WebSocket payload compression            |
| `miniz_oxide`          | 0.8.9   | inflate (backtrace symbolization)        |
| `rustc-demangle`       | 0.1.27  | Panic backtrace symbol demangling        |
| `base64`               | 0.22.1  | Base64 (JWT, auth key)                   |
| `log` + `env_logger`   | —       | Logging                                  |

### Dependency Version Drift

The reconstructed source builds against newer crate versions than the binary.
These produce string differences (library panic paths, version strings) but no
behavioral difference:

| Crate         | Binary | Reconstructed |
| ------------- | ------ | ------------- |
| `tungstenite` | 0.24   | 0.28          |
| `nix`         | 0.29   | 0.31          |
| `clap_lex`    | 0.7    | 1.0           |

## Verification Status

See <PLAN.md> for detailed status.

- [x] Module inventory matches the binary's panic-location table (except `trace.rs`)
- [x] Wire protocol structs match the binary's serde `FIELDS` arrays
- [x] CLI surface matches the binary's clap definition blob
- [x] Recovered source compiles (`bazel build //devinfra/claude/web_env/re/process_api:process_api_re`)
- [ ] `ws_compression.rs` — zstd stream pumps are stubbed (`TODO(re)`)
- [ ] `firecracker_init.rs` CA fan-out — helper bodies are stubbed (`TODO(re)`)
- [ ] `trace.rs` — not recovered at all
- [ ] Binary offsets in `adopter.rs`, `cgroup.rs`, `oom_killer.rs`, `pid_tree.rs`,
      `state.rs` are still carried from older builds
- [ ] Behavioral test harness
- [ ] Behavioral tests pass against the binary
