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

**If a session ever looks like gVisor instead** — 9p root, `runsc` hostname,
sparse `/sys/module`, no `--firecracker-init` in `dmesg` — **stop and tell the
user.** gVisor is historical (pre-2026-06); a sighting means the execution
platform changed unexpectedly. Do not silently fall back to the archived
gVisor workarounds.

## Running dockerd (verified 2026-06-10, dockerd 29.3.1)

A **default `daemon.json` works** — do not copy the gVisor-era workaround
config (it actively breaks things, see below).

```bash
nohup dockerd > /tmp/dockerd.log 2>&1 &
```

- No `iptables`/`ip6tables`/`bridge` disabling needed. dockerd flips
  `net.ipv4.ip_forward=1` itself at startup.
- `data-root` override is unnecessary for correctness — `/` is ext4, not 9p,
  so overlay works anywhere. `/mnt/bazel-tmpfs/docker` remains a fine choice
  for speed/lifetime reasons.
- **Full bridge networking**: user-defined bridges, embedded DNS
  (container-name resolution), container↔container traffic, and
  `extra_hosts: host.docker.internal:host-gateway` (derivation + bridge→host
  traffic) all work.
- **`internal: true` isolation is enforced** — and note the enforcement is
  iptables-based. Under the gVisor-era config (`"iptables": false`) an
  `internal` network silently provided **no** egress-blocking guarantee;
  egress merely happened to fail for lack of NAT. With default config the
  assertion "container on an internal-only network cannot reach `1.1.1.1` or
  the host" passes for the right reason.
- **NAT egress from bridges works**; outbound HTTPS from containers hits the
  TLS-inspecting egress proxy, so containers need the proxy CA or
  `http_proxy`/`https_proxy` env (unchanged from the gVisor era).
- Docker Hub pulls hit unauthenticated rate limits quickly; mirror via
  `public.ecr.aws/docker/library/...` when iterating.
- Verified end-to-end with the `loom/wayback_proxy/compose.yaml` topology
  (one container on an internal-only network whose sole peer is a sidecar on
  internal+egress, upstream reached via `host-gateway`).

Policy unchanged: Bazel `requires_docker` tests still run on RBE (that is
where CI runs them and where `init-dockerd` is provisioned). Local docker is
a viable _debugging_ venue for network-topology tests in current sessions.

## Historical: gVisor era

The 2026-02 evaluation (Podman-vs-Docker workaround comparison, the
`iptables/bridge/data-root` workaround config, ~35-layer overlay limit,
no-bridge-networking constraint) is archived at
<archive/2026*02_docker_gvisor_evaluation.md>. Those findings remain correct
\_on actual gVisor hosts* only.
