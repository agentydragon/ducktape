# bb-box: BuildBuddy VM wrapper

Wrapper around `bb box create` / `bb execute` for spinning up persistent VMs on BuildBuddy executors with configurable
CPU, memory, and disk.

## Quick start

```bash
# SSH box — interactive shell on a Firecracker VM (named = recyclable)
./bb-box.sh my-dev-box

# Connect from another terminal
bb ssh my-dev-box

# Custom sizing
./bb-box.sh --cpus 4 --mem 8GB --disk 20GB my-dev-box

# Custom image
./bb-box.sh --image ghcr.io/agentydragon/rbe-worker:nix-devtools my-dev-box

# Ephemeral (no name = no recycling, VM dies on disconnect)
./bb-box.sh
```

## How it works

1. `bb box create <name>` starts a Firecracker VM with an SSH server inside, connected via WireGuard tunnel through
   BuildBuddy's gateway.
2. With a name, the runner is recycled (`recycle-runner=true`) — reconnecting within the grace period resumes the same
   VM with filesystem state intact.
3. Without a name, the VM is ephemeral.

### Sizing

`bb box create` doesn't expose CPU/memory/disk flags, so the script falls back to `bb execute` when non-default sizing
is requested. In execute mode it:

- Uploads the local `bb` binary as the action input
- Runs `bb ssh-server` inside the executor with the requested exec properties
- Prints the `bb ssh <name>` command to connect

### Exec properties reference

| Property                  | Format         | Example      | Notes                             |
| ------------------------- | -------------- | ------------ | --------------------------------- |
| `EstimatedCPU`            | cores or `m`   | `4`, `4000m` | Millicores or whole cores         |
| `EstimatedMemory`         | IEC bytes      | `8GB`        | Supports `M`, `GB`, `1e3`         |
| `EstimatedFreeDiskBytes`  | IEC bytes      | `20GB`       | Scratch space beyond the image    |
| `recycle-runner`          | `true`/`false` | `true`       | Reuse the same runner across runs |
| `runner-recycling-key`    | string         | `my-box`     | Groups recycled runners           |
| `container-image`         | `docker://...` | see above    | VM container image                |
| `workload-isolation-type` | `firecracker`  | —            | Required for VM isolation         |

### Stateful command execution (no SSH)

For scripting — run commands that share state without SSH:

```bash
KEY=my-session

# First command
bb execute \
  --exec_properties=recycle-runner=true \
  --exec_properties=runner-recycling-key=$KEY \
  --exec_properties=workload-isolation-type=firecracker \
  --exec_properties=container-image=docker://ubuntu:22.04 \
  -- bash -c 'apt-get update && apt-get install -y curl && echo done > /tmp/ready'

# Second command — same runner, /tmp/ready still exists
bb execute \
  --exec_properties=recycle-runner=true \
  --exec_properties=runner-recycling-key=$KEY \
  --exec_properties=workload-isolation-type=firecracker \
  --exec_properties=container-image=docker://ubuntu:22.04 \
  -- bash -c 'cat /tmp/ready && curl -s https://httpbin.org/ip'
```

## Limitations

- **Grace period**: Max 5 minutes after all SSH connections close. The VM is reclaimed after that — reconnect within the
  window to keep it alive.
- **Idle timeout**: Max 5 minutes of SSH inactivity before the session is closed.
- **No port forwarding**: The WireGuard tunnel only carries SSH traffic.
- **`bb box create` sizing**: The upstream `bb box create` command doesn't expose CPU/memory/disk flags. The script
  works around this by using `bb execute` when custom sizing is requested.
