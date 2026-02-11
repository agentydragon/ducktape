# Claude Code Web Container Specification

Runtime context for the Claude Code web environment. The reproducible container
definition lives in the [Dockerfile](../Dockerfile); this file documents the
parts that aren't captured there.

**Captured**: 2026-02-11

## Runtime Environment

| Property     | Value                             |
| ------------ | --------------------------------- |
| OS           | Ubuntu 24.04.3 LTS (Noble Numbat) |
| Kernel       | Linux 4.4.0 (gVisor sandbox)      |
| Architecture | x86_64                            |
| CPUs         | 16                                |
| Memory       | 21Gi                              |
| Disk         | 30G root filesystem               |
| Hostname     | runsc                             |

## Anthropic-Specific Components

Proprietary binaries stored in `binaries/`:

| Binary              | Path                                 | Purpose                                 |
| ------------------- | ------------------------------------ | --------------------------------------- |
| environment-manager | `/usr/local/bin/environment-manager` | Process manager, HTTP proxy             |
| process_api         | `/process_api`                       | Container process API (not snapshotted) |

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
`tools/claude_hooks/proxy_setup.py` for the local proxy workaround.

## gVisor Constraints

- **No `/proc/self/setgroups`**: Use `--annotation run.oci.keep_original_groups=1`. See `scripts/crun-gvisor-wrapper`.
- **SIGPIPE in `podman build`**: buildah's pipe handling causes SIGPIPE. The Dockerfile's `SHELL` directive redirects output to a log file as workaround.
- **No overlay filesystem**: Podman must use VFS storage driver.
- **PTY race condition**: `nix-env` fails due to gVisor EIO on `/dev/ptmx`.
- **No `binfmt_misc`**: APE (Actually Portable Executable) needs local registry.

## Nix

Nix is installed with a minimal store (~30 packages). `sandbox = false` is
required (gVisor breaks the build sandbox). See `tools/claude_hooks/nix_setup.py`.

## Users

| User   | UID  | Home         | Shell     |
| ------ | ---- | ------------ | --------- |
| root   | 0    | /root        | /bin/bash |
| claude | 999  | /home/claude | /bin/bash |
| ubuntu | 1000 | /home/ubuntu | /bin/bash |
