# `bb box` Firecracker VM Workflow

Exploration of BuildBuddy's `bb box` feature: launching Firecracker microVMs, syncing
code into them, running Bazel, getting invocation IDs, and testing reuse/timeout behavior.

Tested from a Claude Code web session (Linux 4.4.0 kernel, HTTPS-only egress proxy).

## What is `bb box`?

`bb box create [name]` starts a long-lived Firecracker VM on BuildBuddy's infrastructure
and launches an SSH server inside it. Under the hood it submits a **24-hour RBE action**
that runs `bb ssh-server` inside a Firecracker VM. Once ready, it prints the SSH connection
details.

```
$ bb box create ducktape-dev
Box: https://app.buildbuddy.io/invocation/266ed2ab-...
Waiting for VM to start...
Box "ducktape-dev" is ready.
  URL:     bb-ssh://[fd00:bb::2]:22?name=ducktape-dev
  Connect: bb ssh ducktape-dev
```

`bb box create` **exits immediately** after printing the connection info — the VM keeps
running independently via the long-lived RBE action.

Named VMs (`bb box create NAME`) use `runner-recycling-key=NAME` so subsequent
`bb box create NAME` reconnects to the same physical VM. Unnamed VMs are ephemeral.

### `bb box` help

```
usage: bb box create [options] [name]

  -api_key string         Override the API key
  -gateway string         Gateway gRPC target (default "grpcs://gateway.buildbuddy.io")
  -grace_period duration  How long the VM stays alive after all SSH connections close
                          (max 5m, default 1m)
  -idle_timeout duration  Close idle SSH sessions (max 5m, default 5m)
  -image string           Container image (default: ubuntu22-04 rbe image)
  -remote_executor string
```

## Setup: Get the BuildBuddy API Key

```bash
# Option A: already set by session start hook
echo $BUILDBUDDY_API_KEY

# Option B: decrypt from SOPS secret
export BUILDBUDDY_API_KEY=$(sops -d --extract '["buildbuddy_api_key"]' \
  secrets/buildbuddy.yaml)
```

## How `bb box` Works Internally (from source)

Source: `cli/box/box.go` at https://github.com/buildbuddy-io/buildbuddy

1. Uploads the local `bb` binary to the remote cache (as the action input root)
2. Submits an RBE action with:
   - `workload-isolation-type=firecracker`
   - `network=external`
   - `container-image=<image>`
   - `recycle-runner=true` + `runner-recycling-key=<name>` (for named boxes)
   - Action timeout: **24 hours**
3. The action runs inside the VM: `./bb record ... ./bb ssh-server --gateway=<gateway>`
4. `bb box create` polls BES event logs for the `bb-ssh://` READY line, then exits
5. The VM keeps running with `bb ssh-server` for up to 24h

The `grace_period` only applies after the LAST SSH client disconnects — while `bb ssh-server`
is running, the VM is alive regardless.

## SSHing In

`bb ssh NAME` connects via a **userspace WireGuard VPN** tunnel through the BB gateway.
Source: `cli/ssh/ssh.go`.

```bash
bb ssh ducktape-dev           # interactive shell
bb ssh ducktape-dev hostname  # run a single command
```

The gateway flow:

1. Generate a local WireGuard keypair (in memory)
2. Call `GatewayService.Register(pubkey)` via gRPC → get assigned VPN IP + WireGuard server endpoint
3. Create userspace TUN using `golang.zx2c4.com/wireguard/tun/netstack` (no kernel module needed)
4. Resolve WireGuard server endpoint hostname → IP via DNS
5. Bring up WireGuard device and send UDP packets to the server
6. Dial SSH through the WireGuard tunnel via `tnet.Dial("tcp", host:22)`

### WireGuard Requirement — Claude Code Web Limitation

**`bb ssh` requires UDP connectivity to the WireGuard server and DNS resolution.**
Both are blocked in the Claude Code web container.

**Why it fails:**

- The container routes all traffic through an HTTPS proxy (`21.0.0.191:15004`)
  — UDP cannot go through an HTTP CONNECT proxy
- `/etc/resolv.conf` is empty; the proxy only resolves hostnames for HTTPS connections
- Even with DNS fixed (adding `8.8.8.8` to `/etc/resolv.conf`), UDP is still blocked

**Error seen:**

```
Warning: wg: Unable to update bind: operation not supported
resolving wg endpoint: lookup gateway-wg.buildbuddy.io on [::1]:53: connection refused
```

**Note**: `bb ssh` uses Go userspace WireGuard (`wireguard-go`) — it does NOT need a kernel
module or kernel 5.6+. The UDP-blocked-by-proxy issue is the actual blocker.

### From a Regular Dev Machine

On macOS or Linux with standard internet access, `bb ssh` works fine:

```bash
bb box create -grace_period=5m mybox
bb ssh mybox                          # interactive shell
bb ssh mybox -- uname -a              # single command
```

**Passing commands**: `bb ssh` accepts remote commands after the host — e.g.
`bb ssh host -- uname -a`. (Do NOT pass after `--`, that parses as bb flags.)

## Code Sync

### Via `bbr` (recommended from Claude Code web)

`bbr` automatically mirrors your local git state (staged + unstaged changes) as patches
applied on the runner. No `git push` required for uncommitted changes.

```bash
bbr build //some:target   # auto-syncs and runs in Firecracker VM
```

### Via git clone inside the VM

```bash
bb execute \
  -remote_header="x-buildbuddy-api-key=$BUILDBUDDY_API_KEY" \
  -exec_properties=workload-isolation-type=firecracker \
  -exec_properties=recycle-runner=true \
  -exec_properties="runner-recycling-key=ducktape-dev" \
  -action_env=HOME=/root \
  -- bash -c '
    git clone --depth=1 https://github.com/agentydragon/ducktape /workspace/ducktape
    echo "Cloned: $(cd /workspace/ducktape && git log --oneline -1)"
  '
```

### Via `bb ssh` + rsync (from regular dev machine)

```bash
bb box create -grace_period=5m mybox
# Once ready:
rsync -av --delete /path/to/ducktape/ mybox:/workspace/ducktape/
# or: git push + git pull inside via bb ssh
```

## Running Bazel — via `bbr` (recommended)

`bbr` wraps `bb remote`, syncs git state automatically, uses the correct container image
(`bbr.json`), and saves the invocation ID.

```bash
cd /path/to/ducktape
bbr build //devinfra/buildbuddy_cli:bbapi
```

**What `bbr` does:**

```
Waiting for available remote runner...
Streaming remote runner logs to: https://app.buildbuddy.io/invocation/7fcd9e0c-...
Syncing existing repo...
$ git fetch --force --depth=1 origin <commit>
$ git apply --verbose <local-diff-patch>   # applies uncommitted local changes
$ bazel build --config=rbe //devinfra/buildbuddy_cli:bbapi
INFO: Invocation ID: aa59a4e0-72a3-46da-abb7-06f156c44399
Build completed successfully, 271 total actions
bbr: invocation 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4
bbr:   targets:   bbapi target 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4
bbr:   logs:      bbapi target log 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4 <target>
bbr:   artifacts: bbapi artifact 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4
bbr:   details:   bbapi invocation 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4
```

## Invocation IDs

```bash
# bbr auto-saves the last invocation ID
INVOCATION_ID=$(cat ~/.cache/bbr/last_invocation_id)
# e.g. 7fcd9e0c-4f47-4604-9176-a2eccca0c1c4

# Inspect the outer (bbr/remote) invocation
bbapi invocation $INVOCATION_ID
# Invocation:  7fcd9e0c-4f47-4604-9176-a2eccca0c1c4
# Status:      COMPLETE_INVOCATION_STATUS
# Command:     remote build //devinfra/buildbuddy_cli:bbapi
# Duration:    8s
# Host:        192.168.241.2   ← Firecracker VM
# Role:        HOSTED_BAZEL
# Success:     true
# Child:       aa59a4e0-72a3-46da-abb7-06f156c44399

# Inspect the inner (Bazel RBE) invocation
bbapi invocation aa59a4e0-72a3-46da-abb7-06f156c44399
# Actions:     271   Duration: 6s   Host: 192.168.241.2

# Browse at: https://app.buildbuddy.io/invocation/<id>
```

Two invocation IDs are produced:

- **Outer** (`bb remote`): use for `bbapi target`, `bbapi artifact`, etc.
- **Inner** (Bazel RBE build): use for action-level cache stats and test results

## VM Environment

Observed via `bb execute` (default Ubuntu 22.04 BB RBE image):

```
OS:        Ubuntu 22.04 LTS (inside Firecracker guest)
Kernel:    Linux 5.15.0 (Firecracker guest kernel)
Hostname:  192.168.241.2  (VM's internal IP)
User:      root
Disk:      49GB total, ~2.5GB used (/dev/vda)
Internet:  yes (public egress, e.g. 216.226.69.6)
HOME:      /  by default — must set HOME=/root for bazelisk to work

Tools available:
  bazelisk  /usr/local/bin/bazelisk  (downloads Bazel on first use)
  git       /usr/bin/git 2.7.4
  python3   /usr/bin/python3 3.6.10  (old! Ubuntu 16.04 base image)
  java      /usr/bin/java
  curl      /usr/bin/curl 7.47.0
  ssh       /usr/bin/ssh
  rsync:    NOT installed in default image
  bb:       NOT installed in default image

NOTE: The default BB image is Ubuntu 16.04 with glibc 2.23. Bazel 9+ requires
glibc 2.25+, so bazelisk fails to run Bazel 9.0.2 in this image.
→ Use bbr (which uses the custom rbe-worker image, Ubuntu 22.04) for Bazel builds.
```

## Timeout and VM Lifetime

### `bb box` VMs (SSH sessions)

| Event                      | Behavior                                        |
| -------------------------- | ----------------------------------------------- |
| `bb box create NAME`       | Starts 24h RBE action with `bb ssh-server`      |
| While SSHed in             | VM runs indefinitely (up to 24h action timeout) |
| After last SSH disconnects | VM stays alive for `grace_period` (max 5m)      |
| During `grace_period`      | New `bb ssh NAME` reconnects to same session    |
| After `grace_period`       | `bb ssh-server` exits, runner is released       |
| Next `bb box create NAME`  | New VM (or potentially same if not yet GC'd)    |

**Observed**: named box created, never SSH'd into → destroyed after ~10 min idle
(grace_period expired). Next `bb box create ducktape-dev` created a new VM (different IP).

### `bbr` / `bb execute` recycled runners

`recycle-runner=true` keeps the Firecracker VM alive between actions, but:

- Each new action gets a **fresh workspace** (workspace dir is cleaned)
- The VM is restored from a snapshot — uptime shows "0 min" each time
- State (files, processes) does NOT persist between `bb execute` calls
- Runner lifetime is managed by BuildBuddy (GC'd after prolonged idle)

**Observed**: two consecutive `bbr build` calls used the same runner (same host `192.168.241.2`),
confirming recycling. A file written in one `bb execute` call was gone in the next.

## Reusing the Same VM

### Via `bb box` (SSH, from regular dev machine)

```bash
bb box create -grace_period=5m mybox
# → Box is ready. Connect: bb ssh mybox

# Reconnect while within grace_period or while action is alive:
bb ssh mybox

# Start a long-lived session (keeps VM alive):
bb ssh mybox
```

### Via `bbr` (stateless, from any environment)

Each `bbr` call reuses the recycled runner automatically:

```bash
bbr build //target1   # runner: 192.168.241.2
bbr build //target2   # same runner: 192.168.241.2
```

### Via `bb execute` with `runner-recycling-key` (from Claude Code web)

Target the same named box without SSH:

```bash
export BB_FLAGS="
  -remote_header=x-buildbuddy-api-key=$BUILDBUDDY_API_KEY
  -exec_properties=workload-isolation-type=firecracker
  -exec_properties=recycle-runner=true
  -exec_properties=runner-recycling-key=ducktape-dev
  -exec_properties=EstimatedFreeDiskBytes=50000000000
  -exec_properties=EstimatedComputeUnits=4
  -action_env=HOME=/root"

bb execute $BB_FLAGS -- bash -c 'hostname && uname -a'
bb execute $BB_FLAGS -- bash -c 'git clone ... && bazelisk build ...'
```

**Important**: this targets the same physical VM (same `runner-recycling-key`) but each
action is independent — workspace is reset, processes don't persist. This is different from
`bb ssh` which gives a continuous shell session with persistent state.

If a `bb box create ducktape-dev` is currently running (ssh-server active), the `bb execute`
with `runner-recycling-key=ducktape-dev` may share the same physical hardware but the
workspace/filesystem state is separate.

## Full Recipe: `bb execute` Without WireGuard

For use from Claude Code web sessions (no WireGuard available):

```bash
export BUILDBUDDY_API_KEY=$(sops -d --extract '["buildbuddy_api_key"]' \
  secrets/buildbuddy.yaml)

# Helper for running commands in a named Firecracker VM
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

# Explore environment
fc_exec bash -c 'uname -a && df -h / && curl -s ifconfig.me'

# Clone + run something
fc_exec bash -c '
  git clone --depth=1 https://github.com/agentydragon/ducktape /workspace/repo
  cd /workspace/repo
  # ... work ...
'
```

## Summary Table

| Goal                     | Command                                                        | Notes                                       |
| ------------------------ | -------------------------------------------------------------- | ------------------------------------------- |
| Create VM                | `bb box create [name]`                                         | Named = recyclable via runner-recycling-key |
| SSH in (regular machine) | `bb ssh name`                                                  | Requires WireGuard (UDP + DNS)              |
| SSH in (Claude Code web) | N/A — use `bb execute`                                         | UDP blocked by HTTPS proxy                  |
| Run command in VM        | `bb execute -exec_properties=runner-recycling-key=name -- cmd` | No WireGuard needed                         |
| Sync code (bbr)          | `bbr build //target` (auto git patch sync)                     | Best for Bazel workflows                    |
| Sync code (git)          | Clone inside VM via `bb execute` or `bb ssh`                   |                                             |
| Sync code (rsync)        | Via `bb ssh` session                                           | Requires `bb ssh` working                   |
| Run Bazel                | `bbr build //target`                                           | Handles API key, invocation ID, git sync    |
| Get invocation ID        | `cat ~/.cache/bbr/last_invocation_id`                          | Auto-saved by `bbr`                         |
| Inspect invocation       | `bbapi invocation <id>`                                        | Shows host, duration, cache stats           |
| VM timeout (`bb box`)    | `grace_period` after last SSH (max 5m), then VM dies           |                                             |
| VM timeout (bbr runner)  | Managed by BuildBuddy — GC'd after prolonged idle              |                                             |
| Persist session state    | `bb box` + `bb ssh` (continuous process)                       | Not possible with `bb execute`              |
