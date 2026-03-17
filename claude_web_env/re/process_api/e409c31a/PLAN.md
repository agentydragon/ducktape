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
- [x] Phase 5: Documentation updated

## Open Items

- [ ] Full vsock listener implementation (requires tokio-vsock in Bazel)
- [ ] UDS WebSocket adapter (requires stream type conversion)
- [ ] JWT validation integration in auth endpoints
- [ ] FIFREEZE/FITHAW ioctl number verification
- [ ] Network ioctl implementation detail (SIOCSIFADDR etc.)
- [ ] Behavioral test harness
