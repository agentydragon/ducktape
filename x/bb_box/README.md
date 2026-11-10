# bb-box: BuildBuddy VM wrapper

Wrapper around `bb box create` / `bb execute` for spinning up Firecracker VMs with configurable CPU, memory, and disk.

## Quick start

```bash
# SSH box — interactive shell on a Firecracker VM
./bb-box.sh my-dev-box

# Connect from another terminal
bb ssh my-dev-box

# Custom sizing
./bb-box.sh --cpus 4 --mem 8GB --disk 20GB my-dev-box

# Custom image
./bb-box.sh --image ghcr.io/agentydragon/rbe-worker:nix-devtools my-dev-box

# Ephemeral (no name)
./bb-box.sh
```

## Wrapper behavior

With no sizing flags, the script delegates to `bb box create`, passing the optional image, name, grace period, and idle timeout. Supplying any of `--cpus`, `--mem`, or `--disk` selects the custom path: it maps them to `EstimatedCPU`, `EstimatedMemory`, and `EstimatedFreeDiskBytes`, then runs one 24-hour `bb execute` action containing `bb ssh-server`. If no name is given on this path, it generates a timestamped recycling key. The shared BuildBuddy lifecycle details are in the [Firecracker workflow](../firecracker_workflow/README.md).

The custom path keeps one SSH server action alive for the box session. In the recorded BB Cloud investigation, separate `bb execute` calls restored fresh VMs and did not share `/tmp`; see the [workflow investigation](../firecracker_workflow/README.md).

## Limits

- Grace period after the last SSH client disconnect and SSH idle timeout default to 5 minutes; see the workflow for lifecycle details.
- No port forwarding; the WireGuard tunnel carries SSH only.
