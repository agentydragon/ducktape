# Docker in Claude Code Sessions (Firecracker)

**Updated:** 2026-06-10
**Environment:** Firecracker microVM, real Linux kernel (6.18.5 at time of
writing), ext4 root. Applies to Claude Code web and CLI-managed remote
sessions.

## Platform identification

Sessions run in Firecracker microVMs. Quick check: `dmesg | head -2` shows
`--firecracker-init` on the kernel command line; `/sys/module` is populated;
root is ext4. The Rust session-start hook does the same check
(`is_firecracker()` in `devinfra/claude/claude_hook/main.rs`, PID 1 cmdline).

If a live web/remote session lacks the Firecracker marker, stop and report
platform drift. Current Docker guidance assumes Firecracker.

## Running dockerd (verified 2026-06-10, dockerd 29.3.1)

A **default `daemon.json` works**.

```bash
nohup dockerd > /tmp/dockerd.log 2>&1 &
```

- No `iptables`/`ip6tables`/`bridge` disabling needed. dockerd flips
  `net.ipv4.ip_forward=1` itself at startup.
- `data-root` override is unnecessary for correctness — `/` is ext4, so
  overlay works anywhere. `/mnt/bazel-tmpfs/docker` remains a fine choice for
  speed/lifetime reasons.
- **Full bridge networking**: user-defined bridges, embedded DNS
  (container-name resolution), container↔container traffic, and
  `extra_hosts: host.docker.internal:host-gateway` (derivation + bridge→host
  traffic) all work.
- **`internal: true` isolation is enforced** by Docker's iptables rules. With
  default config the assertion "container on an internal-only network cannot
  reach `1.1.1.1` or the host" passes for the right reason.
- **NAT egress from bridges works**; outbound HTTPS from containers hits the
  TLS-inspecting egress proxy, so containers need the proxy CA or
  `http_proxy`/`https_proxy` env when explicit proxy variables are present.
- Docker Hub pulls hit unauthenticated rate limits quickly; mirror via
  `public.ecr.aws/docker/library/...` when iterating.
- Verified end-to-end with the `loom/wayback/proxy/compose.yaml` topology
  (one container on an internal-only network whose sole peer is a sidecar on
  internal+egress, upstream reached via `host-gateway`).

Policy unchanged: Bazel `requires_docker` tests still run on RBE (that is
where CI runs them and where `init-dockerd` is provisioned). Local docker is
a viable _debugging_ venue for network-topology tests in current sessions.

## Historical Notes

Older container-runtime experiments are preserved in git history. Current
operator guidance is the Firecracker behavior above.
