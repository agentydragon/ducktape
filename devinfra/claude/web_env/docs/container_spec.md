# Claude Code Web Container Specification

Runtime context for the Claude Code web environment. The reproducible container
definition lives in the [Dockerfile](../Dockerfile); this file documents the
parts that aren't captured there.

**Captured**: 2026-03-16.

## Runtime Environment

| Property     | Value                             |
| ------------ | --------------------------------- |
| OS           | Ubuntu 24.04.3 LTS (Noble Numbat) |
| Kernel       | Linux 6.18.5 (gVisor sandbox)     |
| Architecture | x86_64                            |
| CPUs         | 4                                 |
| Memory       | 15Gi                              |
| Disk         | 252G root filesystem              |
| Hostname     | runsc                             |

Note: Hardware specs vary by container allocation. The kernel version
reflects the gVisor sandbox kernel, not the host kernel.

## Anthropic-Specific Components

Proprietary binaries stored in `reference/`:

| Binary              | Path                                                                  | Purpose                                        |
| ------------------- | --------------------------------------------------------------------- | ---------------------------------------------- |
| environment-manager | `/opt/env-runner/environment-manager` (symlink from `/usr/local/bin`) | Session orchestration, Claude Code lifecycle   |
| process_api         | `/process_api`                                                        | Container init (PID 1), WebSocket API, VM init |

As of 2026-03-16, `process_api` runs with `--firecracker-init` which adds
Firecracker VM initialization (root mount, pivot_root, networking, FUSE,
rclone) before starting the WebSocket listener.

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

## gVisor Constraints

- **No `/proc/self/setgroups`**: Use `--annotation run.oci.keep_original_groups=1`. See `scripts/crun-gvisor-wrapper`.
- **SIGPIPE in `podman build`**: buildah's pipe handling causes SIGPIPE. The Dockerfile's `SHELL` directive redirects output to a log file as workaround.
- **No overlay filesystem**: Podman must use VFS storage driver.
- **PTY race condition**: `nix-env` fails due to gVisor EIO on `/dev/ptmx`.
- **No `binfmt_misc`**: APE (Actually Portable Executable) needs local registry.

## Users

| User   | UID  | Home         | Shell     |
| ------ | ---- | ------------ | --------- |
| root   | 0    | /root        | /bin/bash |
| claude | 999  | /home/claude | /bin/bash |
| ubuntu | 1000 | /home/ubuntu | /bin/bash |
