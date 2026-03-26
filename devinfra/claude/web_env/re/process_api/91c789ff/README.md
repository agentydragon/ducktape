# process_api RE: 91c789ff

Reverse-engineered source for `process_api` Build ID
`91c789ff2a9e647bf7b1914e351f67b89713c4ef`, release `process_api_2026-03-23-22-49`.

See <../README.md> for full protocol documentation, architecture, and CLI reference.

## Binary Properties

| Property     | Value                                             |
| ------------ | ------------------------------------------------- |
| **Build ID** | `91c789ff2a9e647bf7b1914e351f67b89713c4ef`        |
| **Release**  | `process_api_2026-03-23-22-49`                    |
| **Size**     | ~3.2 MB                                           |
| **Linking**  | Static-pie                                        |
| **Modules**  | 10 source files                                   |
| **Stripped** | Yes (no debug info, no symbol table)              |
| **Rust**     | stable `1aa9bab4ecbce4859eaad53000f78158ebe2be2c` |

## Key Features

1. **Firecracker VM Init** (`--firecracker-init`).
   When enabled, `process_api` acts as PID 1 inside a Firecracker microVM:
   - Mount essential filesystems (`/proc`, `/sys`, `/dev`, `/dev/pts`, `/dev/shm`, cgroup2)
   - Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
   - Write `/etc/hostname`, `/etc/hosts`, and `/etc/resolv.conf` (nameserver 8.8.8.8)
     at init time (Binary: 91c789ff)
   - Mount root filesystem from `/dev/vda` (ext4 or squashfs)
   - `pivot_root` or `MS_MOVE`+chroot fallback
   - Mount model tools from `/dev/vdb` (squashfs)
   - Create `/dev/fuse`, spawn rclone for FUSE mounts (`fuse_spawn` log prefix)
   - Write config files (`etc_hosts`, `resolv_conf`, `ca_cert_pem`)
   - Load environment from `/container.env` (JSON)
   - Scrub auth tokens, set system clock, drop `CAP_SYS_RESOURCE`

2. **Vsock support** -- WebSocket listener and control server over
   vsock for Firecracker guest-to-host communication.
   `--listen-vsock-port` and `--control-vsock-port` flags.

3. **UDS support** -- WebSocket listener over Unix domain socket via `--listen-uds`.

4. **Control endpoints** (Binary: 91c789ff):
   - `POST /mount_root` -- apply mount root config (snapstart resume)
   - `POST /fs_free` -- flush buffers, drop caches, FIFREEZE (freeze)
   - `POST /fs_thaw` -- FITHAW (thaw)
   - `POST /auth_public_key/write_etc_files` -- set Ed25519 auth key + write
     `/etc` files; fields: `process_id_reuse`, `allow_process_id`, `memory_limit_bytes`

5. **New WebSocket message variants** (Binary: 91c789ff):
   `KeepAlive`, `Closed`, `AlreadyClosed`, `IoWriteBufferFull`,
   `AttackAttemptUrl`, `HttpFormatIpSocket`; event type `container_shutdown`.

## CLI Flags

| Flag                   | Env Var              | Description                  |
| ---------------------- | -------------------- | ---------------------------- |
| `--addr`               | `ADDR`               | TCP WebSocket listener       |
| `--firecracker-init`   | `FIRECRACKER_INIT`   | Run as Firecracker VM init   |
| `--listen-uds`         | `LISTEN_UDS`         | Unix domain socket WebSocket |
| `--listen-vsock-port`  | `LISTEN_VSOCK_PORT`  | Vsock WebSocket listener     |
| `--control-vsock-port` | `CONTROL_VSOCK_PORT` | Vsock control server         |

## Key Dependencies

| Crate         | Version | Purpose                       |
| ------------- | ------- | ----------------------------- |
| `tokio`       | 1.x     | Async runtime                 |
| `tokio-vsock` | —       | Async vsock streams/listeners |
| `libc`        | —       | Raw syscalls for init path    |

| `jsonwebtoken` | 9.3.1 | JWT authentication (Ed25519) |

## Module Breakdown (10 files)

| Module                | Description                                      |
| --------------------- | ------------------------------------------------ |
| `firecracker_init.rs` | Full VM init system                              |
| `main.rs`             | CLI parsing, vsock/UDS listeners, init path      |
| `control_server.rs`   | HTTP control, mount_root, fs_free/thaw, auth key |
| `io.rs`               | WebSocket I/O multiplexing                       |
| `proc_handle.rs`      | Process handle management                        |
| `oom_killer.rs`       | OOM killer logic                                 |
| `cgroup.rs`           | Cgroup management                                |
| `adopter.rs`          | Orphan process adoption                          |
| `state.rs`            | Process state tracking                           |
| `pid_tree.rs`         | PID tree operations                              |

## Build

```bash
bazel build //devinfra/claude/web_env/re/process_api/91c789ff:process_api_re
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
- FIFREEZE/FITHAW ioctl number verification
- Network ioctl implementation detail (SIOCSIFADDR etc.)
