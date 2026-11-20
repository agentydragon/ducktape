# Firecracker Docker: iptables raw Table Workaround

**Status**: Resolved (2026-02-02) — `74a99ab`
**Environment**: BuildBuddy RBE, Firecracker microVM, Docker 28.1.0

## Problem

Docker 28 added "direct access filtering" — iptables `raw` table PREROUTING
rules when publishing ports (`-p`). Firecracker's guest kernel lacks
`CONFIG_IP_NF_RAW`, so `docker run -p` fails with
`can't initialize iptables table 'raw': Table does not exist`.

Without `-p`, Docker works fine (daemon startup, `docker run`, bridge
networking, inter-container comms all unaffected).

## Fix

Shell wrapper at `/usr/bin/dockerd` sets `DOCKER_INSECURE_NO_IPTABLES_RAW=1`
before exec'ing the real dockerd. This env var (moby
[#49621](https://github.com/moby/moby/pull/49621), Docker 28.0.2+) skips all
raw table operations in `libnetwork/drivers/bridge/port_mapping_linux.go`.

Why a wrapper: BuildBuddy's goinit overwrites `daemon.json` from MMDS metadata
and has no exec property for custom env vars. The wrapper is the only injection
point — goinit calls `dockerd` by name via `exec.LookPath`.

## When to Revisit

- **Firecracker kernel gains `CONFIG_IP_NF_RAW`**: Wrapper becomes unnecessary.
  Test `docker run -p 8080:80 docker.io/library/alpine echo ok` on the stock
  image — if it passes, remove the wrapper.
- **Docker version upgrade past 28.2.0**: Can switch from env var to
  `--allow-direct-routing` CLI flag (moby
  [#49832](https://github.com/moby/moby/pull/49832)), which is more targeted
  (allows raw table for non-routing rules). Not available in 28.1.0.
- **BuildBuddy adds env var support**: If goinit gains an exec property for
  custom dockerd env vars, the wrapper can be removed.

## Debugging Tips

- goinit swallows dockerd stdout/stderr by default — invalid flags cause silent
  30s timeouts. Enable `"test.debug-enable-vm-logs": "true"` exec property for
  `vm_log_tail.txt` (last 12 KB). Only available if the action reaches execution
  phase (pre-execution failures produce empty `test.outputs`).
