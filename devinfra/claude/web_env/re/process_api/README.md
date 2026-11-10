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

Reconstructed source lives under `src/` in this directory. This table
describes the `edebff2c` build; whether the live binary still matches it is
unverified as of 2026-09-12 — see "Capturing the binary" below.

## Capturing the binary

As of 2026-09-12, `/proc/1/exe` and `/proc/1/mem` both return `EACCES` from
inside a live session, for root with full effective capabilities (including
`cap_sys_ptrace`) and no Yama `ptrace_scope` file present. This is a genuine
Firecracker kernel (confirmed via `/proc/version`; not a gVisor-synthesized
`/proc`), so the block is a real kernel/LSM-level access-control decision, not
a sandbox artifact of this repo's own tooling. `/proc/1/maps` and
`/proc/1/smaps_rollup` still work and confirm the binary is fully mapped
(headers/rodata/`.text`/`.data` segments matching the ~4.4 MB size above), so
the bytes exist in memory but no accessible read path reaches them. Forcing a
core dump by signaling PID 1 is not an option — PID 1 is this session's own
container init, so that would take the session down with it. The
`reference/process_api.gz` snapshot predates this restriction (a prior
session could evidently read `/proc/1/exe` directly); there is currently no
known way to recapture `process_api` from inside a session at all.

**Supporting evidence**: `stat /proc/1/exe` shows the symlink itself is wide
open (`0777 lrwxrwxrwx`, owned by `root:root`) — the denial happens inside the
kernel's special-cased handler for that entry, before ordinary DAC permission
bits would matter. `/proc/1/status` shows `PPid: 0` (this really is the
top-level init, not a namespaced view of a fake PID 1) and `Seccomp: 0` (no
seccomp filter on PID 1 itself, so that's not the mechanism).

**Likely mechanism (unconfirmed, ranked by plausibility)** — confirming any
of these would require exactly the ptrace/namespace-comparison probing this
repo's owner declined to pursue against a live session's own PID 1, so this
stays a hypothesis list rather than a finding:

1. **LSM policy (SELinux/AppArmor) specifically labeling `process_api`'s
   memory as protected.** Best fit for `EACCES` — many LSM policy denials
   surface as `EACCES`, where the classic `ptrace_may_access()`
   capability/dumpable-flag failure (no `CAP_SYS_PTRACE` in the target's
   namespace) is remembered as returning `EPERM`. Also the most "on brand"
   explanation given how deliberately layered the rest of the sandbox is.
2. **`prctl(PR_SET_DUMPABLE, 0)` on `process_api` itself**, with `exe`/`mem`
   gated more strictly than `maps` in this kernel/config. Plausible, but sits
   less comfortably with the observed `EACCES` than an LSM denial does.
3. **A user-namespace boundary.** `/proc/self/uid_map` shows an explicit
   `0 0 4294967295` mapping — only present for a _created_ namespace, not the
   initial one — even though it maps uid 0 straight through. If `process_api`
   lives in an ancestor namespace, capabilities that are "full" inside our
   own namespace don't count against a target above it. Architecturally
   plausible for a VM hosting an agent session as a semi-isolated subtree,
   but this failure mode would more naturally surface as `EPERM`.

## Build

```bash
bbr build //devinfra/claude/web_env/re/process_api:process_api_re
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

`process_api` is PID 1. [`src/main.rs`](src/main.rs) wires listeners and shutdown;
[`src/io.rs`](src/io.rs) owns WebSocket process control and I/O, and
[`src/control_server.rs`](src/control_server.rs) owns the HTTP API. Process
state and supervision live in [`src/state.rs`](src/state.rs),
[`src/proc_handle.rs`](src/proc_handle.rs), [`src/cgroup.rs`](src/cgroup.rs),
[`src/oom_killer.rs`](src/oom_killer.rs), [`src/pid_tree.rs`](src/pid_tree.rs),
and [`src/adopter.rs`](src/adopter.rs). Firecracker setup and CA injection are
in [`src/firecracker_init.rs`](src/firecracker_init.rs); Unix UDS/vsock support
is in [`src/platform/unix/mod.rs`](src/platform/unix/mod.rs); zstd support is
partially reconstructed in [`src/ws_compression.rs`](src/ws_compression.rs).

## Module Breakdown

The binary's panic-location table also names `trace.rs`, which has no source
counterpart. Its panic locations are at `.data.rel.ro` `0x4211f0`, `0x421208`,
and `0x421220`; the `##TRACE##` marker is at `0x4211e0`. Current reconstruction
status and remaining work are in [PLAN.md](PLAN.md).

### Function Address Map

Addresses established against the current binary (`edebff2c`) by string
cross-reference plus call-target boundaries. Anything not listed here still
carries a stale address in the source doc comments — see [PLAN.md](PLAN.md).

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

The CLI flags and environment-variable names are defined in
[`src/main.rs`](src/main.rs); [PLAN.md](PLAN.md) records the match against the
binary's clap definition. `--firecracker-init` mounts and pivots to the root,
sets up networking/FUSE and rclone, reads `container.env`, mounts configured
filesystems, installs the egress CA, then spawns the main process. The rclone
VFS cache is `/dev/shm/rclone-vfscache` (`RCLONE_CACHE_DIR`).

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

The full recorded Firecracker invocation, including its vsock listener, is in
[Container Integration](#container-integration).

## WebSocket Protocol

The first text frame is dispatched by its first byte: `{` starts direct
`CreateProcess` or `ProcessConnection` JSON, while `e` starts a JWT (`eyJ...`)
followed by a JSON request. If no auth public key is loaded, JWT is accepted
without verification. Message schemas and dispatch are in
[`src/io.rs`](src/io.rs); reconstructed code is not evidence of runtime parity.

`CreateProcess` supplies a command and optional environment, identity,
reattachment, wall-clock/CPU time, and memory limits. `ProcessConnection`
reattaches or queries a process and can request trace events or zstd. Server
messages are tagged JSON; stdout/stderr use an `ExpectStdOut` or `ExpectStdErr`
text frame followed by binary data, while stdin uses `ExpectStdIn` followed by
binary data. The source enums contain the full field and variant lists.

Local IPs are rejected on WebSocket only when `--block-local-connections` is
enabled. This differs from the control server's unconditional local-peer
rejection below.

### Trace Events

Binary serde evidence gives `TraceEventMsg` five fields: `process`, `host`,
`sph`, `cat`, and `dur_us`; `ProcessConnection.want_trace_events` requests the
stream. `CreateProcess.name` (used as the process ID) cannot contain the
reserved `##TRACE##` marker; the check is in [`src/io.rs`](src/io.rs). The
marker and unrecovered `trace.rs` locations are listed above and in
[PLAN.md](PLAN.md).

### Payload Compression (zstd)

`ProcessConnection.accept_zstd` negotiates `supports_zstd`. Binary parameters:

| Parameter                  | Value                | Evidence               |
| -------------------------- | -------------------- | ---------------------- |
| Compression level          | 3                    | `0x11ff40`             |
| Encoder/decoder window log | 15                   | `0x11ffa2`, `0x14bf5b` |
| Scratch buffer             | 32 KiB               | `0x11ff8b`, `0x14bf44` |
| Decompressed output cap    | 64 MiB (`0x4000000`) | `0x14bfc2`             |

Exceeding the cap reports `decompressed output exceeds 67108864 bytes`.
The stream pumps remain stubbed; see [PLAN.md](PLAN.md) and
[`src/ws_compression.rs`](src/ws_compression.rs) for status and constants.

## HTTP Control Server

Enabling the control server disables the SIGINT handler; shutdown is driven
through HTTP. The source and route handlers are in
[`src/control_server.rs`](src/control_server.rs). Routes are `POST`
`/shutdown`, `/container_name`, `/auth_public_key/write_etc_files`,
`/mount_root`, `/fs_freeze`, `/fs_thaw`, and `/sync_clock`; `GET` `/health` and
`/container_name`; unknown routes return 404. The health route is `/health`;
the binary has no `/healthcheck` path.

`POST /shutdown` calls `sync(1)`, broadcasts shutdown to tracked processes, and
allows a one-second grace period. The key/files endpoint accepts `process`,
`hosts`, and `ca_cert`; CA installation uses the Firecracker helper and returns
500 on helper failure in the reconstructed handler. Its `/health` response is
the `state::debug_process_map` tracked-process summary plus
`Diagnostic info: [OK`; the reconstructed handler does not include process
limits or system PID counts (see [`src/state.rs`](src/state.rs)).

The control server rejects local peers unconditionally. The WebSocket listener
uses `--block-local-connections` for its local-IP filter.

## Cgroup Resource Management

[`src/cgroup.rs`](src/cgroup.rs) detects cgroup v1/v2, creates the
`process_api/` hierarchy, and retries failed setup every 10 seconds. A
per-process child cgroup is created when `memory_limit_bytes` is set; see the
source for controller setup and file details.

## OOM Monitoring

[`src/oom_killer.rs`](src/oom_killer.rs) starts a container monitor when
`--memory-limit-bytes` is set and per-process monitors for requested limits.
The container monitor adopts orphans before scanning usage and signals the
largest process; both monitor paths hand off kills through process channels.

## Process State Machine

[`src/state.rs`](src/state.rs) tracks `Attached`, `Detached`, and `Done`;
reattachable children may remain running after a WebSocket disconnect.
[`src/proc_handle.rs`](src/proc_handle.rs) watches exits, timeouts, and OOM
notifications, then kills the process group/descendants and cleans up its
cgroup. CPU limits use `cpu.stat` `usage_usec`; if that file is unavailable,
`cpu_timeout` is unenforced and only the wall-clock timeout remains.

## PID 1 Duties

[`src/adopter.rs`](src/adopter.rs) polls every five seconds to adopt orphaned
processes and reap zombies with `waitpid(-1, WNOHANG)`.

## Startup Sequence

[`src/main.rs`](src/main.rs) initializes cgroups, starts the orphan monitor and
optional OOM monitor, then binds the configured listener. The control server
and SIGINT handler are mutually exclusive; shutdown kills tracked processes
and allows a one-second grace period.

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

See [environment discovery notes](../../docs/environment_discovery.md) for the full
container boot sequence
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
The behavioral impact of these version differences is unverified; the
version comparison does not establish runtime parity:

| Crate         | Binary | Reconstructed |
| ------------- | ------ | ------------- |
| `tungstenite` | 0.24   | 0.28          |
| `nix`         | 0.29   | 0.31          |
| `clap_lex`    | 0.7    | 1.0           |

## Verification Status

See [PLAN.md](PLAN.md) for per-module evidence, stale offsets, and open work.
The `bbr build` recipe above checks source compilation only; it does not
establish behavior parity with the binary. The zstd stream pumps and CA helper
bodies remain stubbed, `trace.rs` is unrecovered, and there is no behavioral
test harness or test result against the binary. The live binary identity also
remains unverified since the capture restriction described above.
