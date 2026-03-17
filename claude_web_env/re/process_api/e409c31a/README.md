# process_api RE: e409c31a

Reverse-engineered source for `process_api` Build ID
`e409c31a846219e05541706c43daf1756365f486`.

See <../README.md> for full protocol documentation, architecture, and CLI reference.

## Binary Properties

| Property     | Value                                      |
| ------------ | ------------------------------------------ |
| **Build ID** | `e409c31a846219e05541706c43daf1756365f486` |
| **Size**     | 3.2 MB                                     |
| **Linking**  | Static-pie                                 |
| **Modules**  | 10 source files                            |
| **Stripped** | Yes (no debug info, no symbol table)       |

## Key Features

1. **Firecracker VM Init** (`--firecracker-init`) -- 908 lines.
   When enabled, `process_api` acts as PID 1 inside a Firecracker microVM:
   - Mount essential filesystems (`/proc`, `/sys`, `/dev`, `/dev/pts`, `/dev/shm`, cgroup2)
   - Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
   - Mount root filesystem from `/dev/vda` (ext4 or squashfs)
   - `pivot_root` or `MS_MOVE`+chroot fallback
   - Mount model tools from `/dev/vdb` (squashfs)
   - Create `/dev/fuse`, spawn rclone for FUSE mounts
   - Write config files (`etc_hosts`, `resolv_conf`, `ca_cert_pem`)
   - Load environment from `/container.env` (JSON)
   - Scrub auth tokens, set system clock, drop `CAP_SYS_RESOURCE`
   - **Snapstart**: If `/mount_config.json` is absent, enters template mode and
     waits for `POST /mount_root` on the control server

2. **Vsock support** -- WebSocket listener and control server over
   vsock (`tokio-vsock 0.7.2`) for Firecracker guest-to-host communication.
   `--listen-vsock-port` and `--control-vsock-port` flags.

3. **UDS support** -- WebSocket listener over Unix domain socket via `--listen-uds`.

4. **JWT auth token validation** -- `jsonwebtoken 9.3.1` for validating auth
   tokens in FUSE mount configs.

5. **Control endpoints**:
   - `POST /mount_root` -- apply mount root config (snapstart resume)
   - `POST /fs_sync` -- flush buffers, drop caches, FIFREEZE

6. **Container info persistence** -- container name and auth public key
   written to `/container_info.json`.

## CLI Flags

| Flag                   | Env Var              | Description                  |
| ---------------------- | -------------------- | ---------------------------- |
| `--addr`               | `ADDR`               | TCP WebSocket listener       |
| `--firecracker-init`   | `FIRECRACKER_INIT`   | Run as Firecracker VM init   |
| `--listen-uds`         | `LISTEN_UDS`         | Unix domain socket WebSocket |
| `--listen-vsock-port`  | `LISTEN_VSOCK_PORT`  | Vsock WebSocket listener     |
| `--control-vsock-port` | `CONTROL_VSOCK_PORT` | Vsock control server         |

## Key Dependencies

| Crate          | Version | Purpose                       |
| -------------- | ------- | ----------------------------- |
| `tokio`        | 1.x     | Async runtime                 |
| `tokio-vsock`  | 0.7.2   | Async vsock streams/listeners |
| `jsonwebtoken` | 9.3.1   | JWT token validation          |
| `libc`         | --      | Raw syscalls for init path    |

## Module Breakdown (10 files, 4736 lines)

| Module                | Lines | Description                                       |
| --------------------- | ----- | ------------------------------------------------- |
| `firecracker_init.rs` | 908   | Full VM init system                               |
| `main.rs`             | 599   | CLI parsing, vsock/UDS listeners, init path       |
| `control_server.rs`   | 514   | HTTP control, mount_root, fs_sync, vsock, persist |
| `io.rs`               | 1065  | WebSocket I/O multiplexing                        |
| `proc_handle.rs`      | 419   | Process handle management                         |
| `oom_killer.rs`       | 392   | OOM killer logic                                  |
| `cgroup.rs`           | 385   | Cgroup management                                 |
| `adopter.rs`          | 222   | Orphan process adoption                           |
| `state.rs`            | 172   | Process state tracking                            |
| `pid_tree.rs`         | 60    | PID tree operations                               |

## Build

```bash
bazel build //claude_web_env/re/process_api/e409c31a:process_api_re
```

## RE Approach

Ghidra was unavailable in this environment, so analysis used `objdump -d` with
manual string cross-referencing:

1. **String census** -- extracted all `.rodata` strings, identified init-related
   strings (filesystem paths, mount config field names, serde visitor strings,
   networking constants)
2. **LEA cross-reference** -- mapped `.rodata` string addresses back to `.text`
   code via RIP-relative LEA instructions to locate init code at ~0xfb300-0x103000
3. **Struct reconstruction** -- serde visitor strings ("struct FuseMountConfig
   with 10 elements") plus nearby field name strings revealed struct layouts
4. **Function boundary identification** -- function prologues (`push rbp; sub rsp`)
   in the init code range established ~15 function boundaries
5. **Translation** -- decompiled logic translated to idiomatic Rust, maintaining
   `/// Decompiled from 0xAAAA..0xBBBB` annotations where address ranges were
   determined

## Open Items

See <PLAN.md> for full status. Key remaining work:

- Full vsock listener implementation (requires `tokio-vsock` in Bazel)
- UDS WebSocket adapter (requires stream type conversion)
- JWT validation integration in auth endpoints
- FIFREEZE/FITHAW ioctl number verification
- Network ioctl implementation detail (SIOCSIFADDR etc.)
