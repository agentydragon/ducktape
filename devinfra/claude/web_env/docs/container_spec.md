# Claude Code Web Container Specification

Runtime context for the Claude Code web environment. The reproducible container
definition lives in the [Dockerfile](../Dockerfile); this file documents the
parts that aren't captured there.

**Captured**: 2026-03-30.

## Runtime Environment

| Property     | Value                                         |
| ------------ | --------------------------------------------- |
| OS           | Ubuntu 24.04.3 LTS (Noble Numbat)             |
| Kernel       | Linux 6.18.5 (real kernel on Firecracker)     |
| Architecture | x86_64                                        |
| CPU          | Intel Xeon @ 2.10GHz (Granite/Emerald Rapids) |
| CPUs         | 4 (no hyperthreading)                         |
| Memory       | 16Gi (no cgroup limit)                        |
| Swap         | None                                          |
| Root disk    | 252G ext4 on `/dev/vda` (virtio)              |
| Hostname     | vm                                            |
| Hypervisor   | KVM (Firecracker microVM)                     |
| Init         | `/process_api --firecracker-init`             |

### Storage Layout

| Mountpoint         | Source     | FS Type  | Size  | Purpose                  |
| ------------------ | ---------- | -------- | ----- | ------------------------ |
| `/`                | `/dev/vda` | ext4     | 252G  | Root filesystem          |
| `/opt/claude-code` | `/dev/vdb` | squashfs | 60.8M | Claude Code binary (ro)  |
| `/opt/env-runner`  | `/dev/vdc` | squashfs | 17.5M | Environment manager (ro) |
| `/dev/shm`         | tmpfs      | tmpfs    | 7.9G  | Shared memory            |

The Bazel cache (`~/.claude/session-env/<id>/bazel-cache`) lives on the ext4
root disk. There are **no tmpfs mounts** for Bazel cache or container storage.

### IO Benchmarks (2026-03-30)

Measured with `dd` on a Firecracker microVM (4 vCPU, 16Gi RAM).

| Location      | FS Type | Seq Write | Seq Read | 4K Write (IOPS) |
| ------------- | ------- | --------- | -------- | --------------- |
| `/` (disk)    | ext4    | 98 MB/s   | 241 MB/s | 92 MB/s         |
| `/dev/shm`    | tmpfs   | 345 MB/s  | 2.4 GB/s | 1.3 GB/s        |
| `/tmp` (disk) | ext4    | 118 MB/s  | 422 MB/s | 94 MB/s         |

Disk I/O is adequate for Bazel cache (sequential reads dominate). tmpfs is
~3-10x faster but consumes RAM. On the old gVisor/9p root, disk was ~10x
slower, making tmpfs essential. On Firecracker ext4, tmpfs is unnecessary.

### Historical Note: gVisor → Firecracker Migration

As of 2026-03-30, the environment runs on **Firecracker microVMs with a real
Linux kernel**, not gVisor. The root filesystem is ext4 on a virtio block
device, not 9p. The session start hook detects the platform at runtime
(`hook_daemon/session_start/platform_detect.py`) and adapts:

- **Firecracker**: skips tmpfs mounts, uses overlay Docker storage driver
  natively, sizes JVM heap to 8Gi for full-monorepo Skyframe analysis.
- **gVisor** (legacy): mounts tmpfs for fast I/O, uses overlay-on-tmpfs or
  vfs fallback, uses 4Gi JVM heap to leave room for tmpfs.
- **Unknown platform**: logs a warning asking the agent to notify the user,
  uses conservative defaults (no tmpfs, 4Gi heap).

### Bazel JVM Heap

Java auto-sizes max heap to ~25% of physical memory (~4Gi). For full-monorepo
`bazel query` operations (6000+ packages), this is insufficient — the Skyframe
analysis cache alone needs ~4Gi. The session bazelrc template sets `-Xmx8g` on
Firecracker (where disk-backed cache doesn't compete for RAM) and `-Xmx4g` on
gVisor (where tmpfs eats into available memory).

## Anthropic-Specific Components

Proprietary binaries stored in `reference/`:

| Binary              | Path                                                                  | Purpose                                        |
| ------------------- | --------------------------------------------------------------------- | ---------------------------------------------- |
| environment-manager | `/opt/env-runner/environment-manager` (symlink from `/usr/local/bin`) | Session orchestration, Claude Code lifecycle   |
| process_api         | `/process_api`                                                        | Container init (PID 1), WebSocket API, VM init |

`process_api` runs with `--firecracker-init` which handles Firecracker VM
initialization (root mount, pivot_root, networking, FUSE, rclone) before
starting the WebSocket listener.

The `sandbox-runtime` (`@anthropic-ai/sandbox-runtime`) is now **open source**
at <https://github.com/anthropic-experimental/sandbox-runtime>.

`/container_info.json` contains per-session metadata (`container_name`,
`creation_time`).

### Git Commit Signing

Commits are signed via SSH key at `/home/claude/.ssh/commit_signing_key.pub`
using `/tmp/code-sign` as the gpg ssh program. See `config/gitconfig`.

### Claude Code Settings

Runtime settings in `/root/.claude/settings.json` and
`/home/claude/.claude/settings.json` — see `config/global-settings.json` and
`config/sandbox-settings.json`.

## Network Architecture

Outbound HTTP/HTTPS is proxied through `environment-manager`. The proxy performs
TLS inspection, injecting its own CA certificate. Proxy credentials are
JWT-based, passed via `Proxy-Authorization: Basic` header.

The proxy returns non-standard HTTP 401 (not 407) with `www-authenticate` (not
`Proxy-Authenticate`), which breaks Java's `Authenticator` class. See
`devinfra/claude/proxy_setup.py` for the local proxy workaround.

| Interface | IP Address   |
| --------- | ------------ |
| lo        | 127.0.0.1    |
| eth0      | 192.0.2.2/24 |

Default route via 192.0.2.1. IPv6 disabled (`ipv6.disable=1`).

## Installed Software

| Tool    | Version                 |
| ------- | ----------------------- |
| Python  | 3.11.14 (system)        |
| Java    | OpenJDK 21.0.10+7       |
| Node.js | 22.22.0                 |
| Go      | 1.24.7                  |
| GCC     | 13.3.0                  |
| Git     | 2.43.0                  |
| Bazel   | 8.6.0 (via bazelisk)    |
| Nix     | 2.28.3                  |
| Docker  | 29.2.1 (CLI + BuildKit) |

Nix store: ~2.9Gi on root disk.

## Users

| User   | UID  | Home         | Shell     |
| ------ | ---- | ------------ | --------- |
| root   | 0    | /root        | /bin/bash |
| claude | 999  | /home/claude | /bin/bash |
| ubuntu | 1000 | /home/ubuntu | /bin/bash |
