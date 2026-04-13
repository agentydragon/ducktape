# `bb box` / `bbr` Firecracker VM Workflow

Exploration of BuildBuddy's Firecracker microVM features: `bb box` (persistent SSH-accessible
dev boxes), `bbr` (Remote Bazel with warm snapshot recycling), and `bb execute` (raw RBE
commands). Tested from a Claude Code web session (Linux 4.4.0 kernel, HTTPS-only egress proxy).

## What BuildBuddy Offers

| Feature             | What it is                                               | State persistence                                |
| ------------------- | -------------------------------------------------------- | ------------------------------------------------ |
| `bb box`            | Long-lived Firecracker VM with SSH server (24h lifetime) | Full — persistent process                        |
| `bbr` / `bb remote` | Remote Bazel with warm snapshot recycling                | Full VM memory — warm Bazel server across builds |
| `bb execute`        | Ad-hoc RBE commands, no snapshot recycling               | None — fresh VM each call                        |

---

## `bb box` — Persistent Dev Boxes

`bb box create [name]` submits a **24-hour RBE action** running `bb ssh-server` inside a
Firecracker VM, then exits immediately. The VM keeps running independently.

```
$ bb box create ducktape-dev
Box: https://app.buildbuddy.io/invocation/266ed2ab-...
Waiting for VM to start...
Box "ducktape-dev" is ready.
  URL:     bb-ssh://[fd00:bb::2]:22?name=ducktape-dev
  Connect: bb ssh ducktape-dev
```

Named boxes (`bb box create NAME`) set `runner-recycling-key=NAME`, so `bb box create NAME`
again reconnects to the same physical VM. Unnamed boxes are ephemeral.

### `bb box` internals (from `cli/box/box.go`)

1. Uploads the local `bb` binary to the remote cache
2. Submits an RBE action with `workload-isolation-type=firecracker`, `network=external`,
   `recycle-runner=true`, `runner-recycling-key=<name>`, timeout 24h
3. Action runs inside VM: `./bb ssh-server --gateway=... --grace_period=...`
4. `bb box create` polls BES logs for the `bb-ssh://` READY line, then exits
5. VM stays alive with `bb ssh-server` for up to 24h

`grace_period` (max 5m, default 1m) only applies after the **last SSH client disconnects**.
While `bb ssh-server` is running, the VM is alive regardless.

### Timeout behavior

| Event                      | Behavior                                        |
| -------------------------- | ----------------------------------------------- |
| `bb box create NAME`       | Starts 24h RBE action with `bb ssh-server`      |
| While SSHed in             | VM runs indefinitely (up to 24h action timeout) |
| After last SSH disconnects | VM stays alive for `grace_period` (max 5m)      |
| During `grace_period`      | New `bb ssh NAME` reconnects to same session    |
| After `grace_period`       | `bb ssh-server` exits, runner is released       |
| Next `bb box create NAME`  | New VM (or same if runner not yet GC'd)         |

**Observed**: named box created, never SSH'd into → destroyed after ~10 min idle
(grace_period expired). Next `bb box create ducktape-dev` created a new VM (different IP).

### `bb ssh` — WireGuard requirement

`bb ssh NAME` tunnels through a userspace WireGuard VPN (source: `cli/ssh/ssh.go`):

1. Generate local WireGuard keypair in memory
2. Call `GatewayService.Register(pubkey)` via gRPC → get VPN IP + server endpoint
3. Create userspace TUN via `golang.zx2c4.com/wireguard/tun/netstack` (no kernel module needed)
4. Resolve WireGuard server endpoint via DNS, bring up WireGuard, send UDP
5. Dial SSH through the WireGuard TUN

**Claude Code web limitation**: all traffic routes through an HTTPS proxy (`21.0.0.191:15004`).
UDP cannot traverse an HTTP CONNECT proxy, and DNS for raw sockets is also broken. `bb ssh`
fails with:

```
Warning: wg: Unable to update bind: operation not supported
resolving wg endpoint: lookup gateway-wg.buildbuddy.io on [::1]:53: connection refused
```

From a regular dev machine (macOS/Linux with standard internet), `bb ssh` works fine:

```bash
bb box create -grace_period=5m mybox
bb ssh mybox                   # interactive shell
bb ssh mybox hostname          # single command
```

---

## `bbr` — Remote Bazel with Warm Snapshot Recycling

`bbr` wraps `bb remote` and is the recommended way to run Bazel from Claude Code web. It:

- Auto-syncs local git diffs as patches (no `git push` needed for uncommitted changes)
- Uses the custom `rbe-worker` Ubuntu 22.04 image (configured in `devinfra/bbr.json`)
- Saves the invocation ID to `~/.cache/bbr/last_invocation_id`
- Supports **Firecracker snapshot recycling** — the running Bazel server (JVM, analysis
  cache, output base) survives across builds

```bash
bbr build //devinfra/buildbuddy_cli:bbapi
```

### Snapshot recycling — how it works

Source: BB docs at <https://www.buildbuddy.io/docs/remote-runner-introduction> and
`enterprise/server/remote_execution/containers/firecracker/firecracker.go`.

The Firecracker process is **fully stopped and restarted** from a snapshot each time — it is
not kept alive. The lifecycle per build:

1. Action runs inside the VM (Bazel build, git sync, etc.)
2. `Pause()`: workspace drive is swapped to an empty placeholder; full or diff memory snapshot
   is saved to BB's remote cache; Firecracker process is killed
3. Next build with the same snapshot key: new Firecracker process starts, snapshot is loaded
   (lazily via UFFD), VM resumes from exact saved state

**What is preserved in the snapshot:**

- Full VM memory → running Bazel server survives with in-memory analysis cache
- Root filesystem (`/tmp`, `/root`, `/root/.cache/bazel/`) → bazelisk downloads, Bazel output base
- The workspace drive (`/workspace/`) is **excluded** from the snapshot (repacked per-action)

**`boot_id` always changes on snapshot restore** — Firecracker intentionally regenerates
entropy-related state per clone to prevent `boot_id` collisions
(see Firecracker's `docs/snapshotting/random-for-clones.md`). It is NOT evidence of a cold
boot or a different runner.

### Snapshot key and policies

The snapshot is keyed on: remote instance name, platform property hash (all
`runner_exec_properties`), VM config (CPUs, memory, disk), and git branch. Any change to
these forces a cold start.

Snapshot behavior is controlled by two `runner_exec_properties`:

| Property                      | Default                 | Options                                             |
| ----------------------------- | ----------------------- | --------------------------------------------------- |
| `remote-snapshot-save-policy` | `first-non-default-ref` | `always`, `first-non-default-ref`, `none-available` |
| `snapshot-read-policy`        | `newest`                | `newest`, `local-first`, `local-only`               |

**Default `first-non-default-ref`**: saves a snapshot only on the first run for a non-default
branch, and always on the default branch. Subsequent runs on the same branch read the existing
snapshot but don't write a new one.

**For maximum warm-runner benefit** (interactive development):

```bash
BBR_REMOTE_ARGS="--runner_exec_properties=remote-snapshot-save-policy=always \
                 --runner_exec_properties=snapshot-read-policy=newest" \
  bbr build //target
```

Or add to `BBR_REMOTE_ARGS` in your shell profile for persistent effect.

### Observed build times

Three consecutive `bbr build //devinfra/buildbuddy_cli:bbapi` with
`remote-snapshot-save-policy=always`:

| Build | Snapshot state        | Bazel elapsed | Packages loaded      |
| ----- | --------------------- | ------------- | -------------------- |
| 1     | Cold (no snapshot)    | 8.055s        | 1                    |
| 2     | Warm (snapshot found) | 1.284s        | 1                    |
| 3     | Warm (same snapshot)  | 0.652s        | **0** (fully cached) |

Build 3 with "0 packages loaded, 0 targets configured" is a fully warm Bazel server — Bazel
re-uses the in-memory analysis cache with zero analysis work.

Without the explicit save policy (default `first-non-default-ref`), consecutive back-to-back
builds may both cold-start due to a snapshot serialization race (snapshot not yet written to
cache before the second build starts) or landing on a different executor (local snapshot
inaccessible from a different machine). Fallback chain: exact branch snapshot → base branch →
default branch → fresh cold boot.

---

## `bb execute` — Ad-hoc RBE Commands

`bb execute` runs a single RBE action. **No snapshot recycling** — every call cold-boots a
fresh Firecracker VM (~4-5s uptime). Useful for one-off commands from Claude Code web.

```bash
export BUILDBUDDY_API_KEY=$(sops -d --extract '["buildbuddy_api_key"]' \
  secrets/buildbuddy.yaml)

bb execute \
  -remote_header="x-buildbuddy-api-key=$BUILDBUDDY_API_KEY" \
  -exec_properties=workload-isolation-type=firecracker \
  -exec_properties=recycle-runner=true \
  -exec_properties="runner-recycling-key=ducktape-dev" \
  -exec_properties=EstimatedFreeDiskBytes=50000000000 \
  -exec_properties=EstimatedComputeUnits=4 \
  -action_env=HOME=/root \
  -- bash -c 'hostname && uname -a && curl -s ifconfig.me'
```

`runner-recycling-key` routes calls to the same physical executor, but each action still
gets a fresh VM snapshot restore (workspace cleared, `/tmp` cleared, new `boot_id`). The
`preserve-workspace=true` exec property exists in the BB source and is supposed to preserve
non-output workspace files between recycled calls, but has no observable effect on BB Cloud
for `bb execute` (workspace is always `lost+found`-only on every call).

### Helper for repeated commands

```bash
fc_exec() {
  bb execute \
    -remote_header="x-buildbuddy-api-key=$BUILDBUDDY_API_KEY" \
    -exec_properties=workload-isolation-type=firecracker \
    -exec_properties=recycle-runner=true \
    -exec_properties="runner-recycling-key=ducktape-dev" \
    -exec_properties=EstimatedFreeDiskBytes=50000000000 \
    -exec_properties=EstimatedComputeUnits=4 \
    -action_env=HOME=/root \
    -action_env=XDG_CACHE_HOME=/root/.cache \
    -- "$@"
}

fc_exec bash -c 'uname -a && df -h / && curl -s ifconfig.me'
fc_exec bash -c '
  git clone --depth=1 https://github.com/agentydragon/ducktape /workspace/repo
  cd /workspace/repo && bazelisk build //devinfra/buildbuddy_cli:bbapi
'
```

---

## Setup: Get the BuildBuddy API Key

```bash
# Option A: already set by session start hook
echo $BUILDBUDDY_API_KEY

# Option B: decrypt from SOPS secret
export BUILDBUDDY_API_KEY=$(sops -d --extract '["buildbuddy_api_key"]' \
  secrets/buildbuddy.yaml)
```

---

## Invocation IDs

`bbr` produces two invocation IDs:

```bash
# Outer (bb remote / runner invocation) — auto-saved by bbr
OUTER=$(cat ~/.cache/bbr/last_invocation_id)
bbapi invocation $OUTER
# Invocation:  7fcd9e0c-...
# Duration:    8s   Host: 192.168.241.2   Role: HOSTED_BAZEL
# Child:       aa59a4e0-...   ← inner Bazel RBE invocation

# Inner (Bazel RBE build) — use for action cache stats, test results
bbapi invocation aa59a4e0-...
# Actions: 271   Duration: 6s   AC Hits: 865

# Browse: https://app.buildbuddy.io/invocation/<id>
```

`bbapi target` and `bbapi target log` auto-resolve outer IDs to inner — either works.

---

## VM Environment

Observed via `bb execute` with default BB RBE image:

```
OS:       Ubuntu 16.04 LTS (default BB RBE image — glibc 2.23, Bazel 9 won't run)
OS:       Ubuntu 22.04 LTS when using rbe-worker image (bbr / -exec_properties=container-image=...)
Kernel:   Linux 5.15.0 (Firecracker guest)
User:     root
HOME:     / by default — must set -action_env=HOME=/root for bazelisk to work
Disk:     49 GB total, ~2.5 GB used
Internet: yes (public egress)

Tools (default image):  git 2.7.4, bazelisk, java, curl — no rsync, no bb binary
```

---

## Summary: State Persistence by Access Method

| Method                                          | State preserved                    | Works from Claude Code web     | Notes                                                             |
| ----------------------------------------------- | ---------------------------------- | ------------------------------ | ----------------------------------------------------------------- |
| `bb box` + `bb ssh`                             | Full persistent process            | **No** — WireGuard/UDP blocked | Works from regular dev machine                                    |
| `bbr` with `remote-snapshot-save-policy=always` | Full VM memory (Bazel server warm) | **Yes**                        | Build 3 showed 0 packages loaded, 0.652s                          |
| `bbr` with default policy                       | Maybe warm, maybe cold             | Yes                            | Race condition / different executor → often cold                  |
| `bb execute`                                    | None                               | Yes                            | Fresh VM every call; `preserve-workspace` ineffective on BB Cloud |

`boot_id` is **not** a reliable indicator of snapshot reuse — it always changes on Firecracker
snapshot restore by design (entropy regeneration per clone). Use Bazel analysis time and
"packages loaded" count as the actual warm-vs-cold indicator.
