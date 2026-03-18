# e409c31a RE Plan

## Binary Info

| Property         | Value                                      |
| ---------------- | ------------------------------------------ |
| **Build ID**     | `e409c31a846219e05541706c43daf1756365f486` |
| **Size**         | 3.2 MB                                     |
| **Linking**      | Static-pie                                 |
| **Source files** | 10 modules                                 |

## Features

### 1. Firecracker VM Init (`--firecracker-init`)

The largest module. When enabled, process_api acts as a full VM init
system (PID 1) inside a Firecracker microVM:

- Mount essential filesystems (/proc, /sys, /dev, /dev/pts, /dev/shm, cgroup2)
- Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
- Mount root filesystem from /dev/vda (ext4 or squashfs)
- pivot_root or MS_MOVE+chroot fallback
- Mount model tools from /dev/vdb (squashfs)
- Create /dev/fuse, spawn rclone for FUSE mounts
- Mount rclone_tools from block device
- Mount readonly block devices
- Write config files (etc_hosts, resolv_conf, ca_cert_pem)
- Load environment variables from /container.env (JSON)
- Scrub auth tokens from saved configs
- Set system clock via clock_settime
- Drop CAP_SYS_RESOURCE
- Write /proc/sys/vm/drop_caches

**Snapstart support**: If /mount_config.json doesn't exist at boot, the init
enters snapstart template mode and signals SNAPSTART_READY. The mount root
config is then supplied via POST /mount_root on the control server.

### 2. CLI Flags

| Flag                   | Env Var              | Description                            |
| ---------------------- | -------------------- | -------------------------------------- |
| `--firecracker-init`   | `FIRECRACKER_INIT`   | Run as Firecracker VM init             |
| `--listen-uds`         | `LISTEN_UDS`         | Unix domain socket WebSocket listener  |
| `--listen-vsock-port`  | `LISTEN_VSOCK_PORT`  | Vsock WebSocket listener (Firecracker) |
| `--control-vsock-port` | `CONTROL_VSOCK_PORT` | Vsock control server (Firecracker)     |

Note: `--addr` is Optional since vsock/UDS are alternatives.

### 3. Control Server Endpoints

| Method | Path          | Description                                               |
| ------ | ------------- | --------------------------------------------------------- |
| `POST` | `/mount_root` | Apply mount root config (enabled with --firecracker-init) |
| `POST` | `/fs_sync`    | Flush buffers, drop caches, FIFREEZE                      |

### 4. Vsock Support

Both the WebSocket listener and control server can operate over vsock
(Firecracker's virtio socket interface) instead of TCP. Uses `tokio-vsock 0.7.2`.
Connections validated against CID == 2 (host).

### 5. UDS Support

WebSocket listener can operate over a Unix domain socket via `--listen-uds`.

### 6. JWT Auth Token Validation

Uses `jsonwebtoken 9.3.1`. TokenClaims struct with `sub`, `iat`,
`exp` fields. Validates auth tokens in FUSE mount configs.

### 7. Container Info Persistence

Container name and auth public key are persisted to `/container_info.json`
when updated via control server.

## Key Dependencies

| Crate          | Version | Purpose                       |
| -------------- | ------- | ----------------------------- |
| `tokio-vsock`  | 0.7.2   | Async vsock streams/listeners |
| `jsonwebtoken` | 9.3.1   | JWT token validation          |
| `libc`         | —       | Raw syscalls for init path    |

## Completed

- [x] Phase 1: String census and diff analysis
- [x] Phase 1: Section comparison (static-pie vs dynamic)
- [x] Phase 1: INIT string extraction and cataloging
- [x] Phase 1: New CLI flag identification
- [x] Phase 1: Source file path extraction (src/firecracker_init.rs)
- [x] Phase 2: objdump cross-reference analysis for init code (0xfb394..0x103000)
- [x] Phase 3: firecracker_init.rs reconstruction (MountRootConfig, FuseMountConfig, full init sequence)
- [x] Phase 3: main.rs updated (new CLI flags, firecracker-init code path, UDS/vsock listeners)
- [x] Phase 3: control_server.rs updated (mount_root, fs_sync, vsock, persist)
- [x] Phase 4: BUILD.bazel updated (new source file, new deps, static-pie note)
- [x] Phase 4: FIFREEZE/FITHAW ioctl values verified (0xC0045877, 0xC0045878 = `_IOWR('X', 119/120, int)`)
- [x] Phase 4: `create_device_nodes()` implemented and wired into `mount_essential_filesystems()`
- [x] Phase 4: `scrub_auth_tokens()` verified complete (scrubs `/mount_config.json` and `/tmp/rclone-mount-config.json`)
- [x] Phase 4: Binary string comparison (1775 strings extracted, cross-referenced with source)
- [x] Phase 5: Documentation updated

## Phase 4 String Comparison Findings

### FIFREEZE/FITHAW Verification

Confirmed correct against Linux kernel headers:

- `FIFREEZE = _IOWR('X', 119, int) = 0xC0045877`
- `FITHAW = _IOWR('X', 120, int) = 0xC0045878`

### Device Nodes

Binary string evidence shows only 4 device paths as standalone literals:
`/dev/null`, `/dev/random`, `/dev/urandom`, `/dev/tty`. Updated
`create_device_nodes()` to match (removed `/dev/zero`, `/dev/console`,
`/dev/ptmx` which were speculative). Now called from
`mount_essential_filesystems()`.

### JWT Authentication (NOT YET IMPLEMENTED)

Major missing functionality. The binary contains a full JWT authentication
flow in the WebSocket connection handler (`io.rs`), guarded by the
`/auth_public_key` control server endpoint. Key strings found in binary
but not in source:

**WebSocket JWT flow** (first message routing):

- `"[DEBUG] Unexpected first byte '"`
- `"': expected '{' (JSON) or 'e' (JWT)"`
- `"[DEBUG] Received JWT token, verifying..."`
- `"[DEBUG] JWT verified successfully: sub='"`
- `"[DEBUG] JWT verification failed: "`
- `"[DEBUG] No auth public key loaded, accepting JWT without verification"`
- `"[DEBUG] Received ProcessConnection JSON (no JWT)"`
- `"[DEBUG] Failed to get ProcessConnection after JWT: "`
- `"Client closed connection after JWT"`
- `"Second message after JWT should be text json CreateProcess"`
- `"Empty first message"`

**JWT validation errors** (from jsonwebtoken crate):

- `"Invalid JWT claims: "`, `"JWT authentication failed: "`
- `"JWT decode error: "`, `"JWT key error: "`
- `"Invalid JWT signature"`, `"JWT token has expired"`

**Control server auth endpoint** (`POST /auth_public_key`):

- `"[CONTROL] Auth public key set successfully"`
- `"[CONTROL] Invalid auth public key: "`
- `"Invalid base64 for auth public key: "`
- `"Ed25519 public key must be exactly 32 bytes, got "`
- `"Auth public key must be exactly 32 bytes (raw Ed25519), got "`
- `"[CONTROL] Failed to persist auth key to container_info.json: "`
- `"[WARN] Failed to load auth key:"`

**Implications**: The first WebSocket message is inspected by its first byte:
`'{'` routes to JSON (CreateProcess/ProcessConnection), `'e'` routes to JWT
token (base64url-encoded JWT starts with `ey`). After JWT verification, a
second message carries the actual CreateProcess/ProcessConnection JSON. The
auth public key is an Ed25519 key set via the control server and persisted
to `/container_info.json`.

### Control Server Vsock CID Validation

Binary string found: `"[CONTROL] [SECURITY] Rejected connection from non-host CID "`.
Not in source (vsock control server is a stub). Requires tokio-vsock.

### tokio-vsock and jsonwebtoken Crate Status

Neither `tokio-vsock` nor `jsonwebtoken` exist in the root `Cargo.toml`.
They remain commented out in `BUILD.bazel`. Adding them requires:

1. Add crate entries to root `Cargo.toml`
2. Run `CARGO_BAZEL_REPIN=1 bazel build @crates//:all`
3. Uncomment deps in `BUILD.bazel`

Binary confirms versions via panic paths:

- `jsonwebtoken-9.3.1` (at `artifactory.infra.ant.dev-7db23613d841872b`)
- `tokio-vsock-0.7.2` (at `artifactory.infra.ant.dev-7db23613d841872b`)
- `ring-0.17.14` (Ed25519 signing, dependency of jsonwebtoken)

## Open Items

- [ ] Full vsock listener implementation (requires tokio-vsock in `Cargo.toml` + Bazel repin)
- [ ] JWT authentication flow in `io.rs` WebSocket handler (requires jsonwebtoken in `Cargo.toml` + Bazel repin)
- [ ] `POST /auth_public_key` endpoint in `control_server.rs` (Ed25519 key, base64-encoded)
- [ ] Auth key persistence/loading from `/container_info.json`
- [ ] Network ioctl implementation detail (SIOCSIFADDR etc.)
- [ ] Behavioral test harness
