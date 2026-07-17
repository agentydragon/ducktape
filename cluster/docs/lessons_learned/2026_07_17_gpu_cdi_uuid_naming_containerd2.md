# GPU pods fail with "unresolvable CDI devices" after containerd 2.x

- **Date:** 2026-07-17
- **Trigger:** nixpkgs 26.05 switch on wyrm2 (bumped containerd 1.x → 2.3.0)

## Symptom

GPU pods on wyrm2 crash at container creation (never start), `CrashLoopBackOff`
with `StartError`:

```text
failed to create shim task: OCI runtime create failed: could not apply required
modification to OCI specification: error modifying OCI spec: failed to inject CDI
devices: unresolvable CDI devices nvidia.com/gpu=GPU-690929fc-..., nvidia.com/gpu=GPU-6154a49f-...
```

## Root cause

Two facts combine:

1. The NixOS `nvidia-cdi-generator` runs `nvidia-ctk cdi generate
--device-name-strategy index`, so `/var/run/cdi/nvidia-container-toolkit.json`
   names devices `0`, `1`, `all` — **no UUID entries**. This is set by the
   `hardware.nvidia-container-toolkit.device-name-strategy` option, whose NixOS
   default is `"index"`. (nvidia-ctk's _own_ default is `index` **and** `uuid`;
   the k8s device plugin's default `deviceIDStrategy` is `uuid`.)
2. The `nvidia-device-plugin` (deviceIDStrategy=uuid) references allocated GPUs
   by UUID: `nvidia.com/gpu=GPU-<uuid>`.

On **containerd 1.x**, CDI was off by default: GPU injection went through the
`nvidia-container-runtime.cdi` handler (RuntimeClass `nvidia`) reading
`NVIDIA_VISIBLE_DEVICES`, and the plugin's UUID CDI references were ignored — so
the index-only spec never mattered.

**containerd 2.x enables CDI by default.** It now resolves the plugin's UUID CDI
references against the spec — which only has index names — so injection fails and
the container never starts.

## Fix

Set the CDI generator to emit UUID device names, matching the plugin and keeping
stable device identity (in `nix/nixos/hosts/wyrm2/default.nix`):

```nix
hardware.nvidia-container-toolkit = {
  enable = true;
  device-name-strategy = "uuid";
};
```

Apply with `sudo nixos-rebuild switch`, then verify:

```bash
# spec now lists GPU-<uuid> device names
python3 -c 'import json;print([d["name"] for d in json.load(open("/var/run/cdi/nvidia-container-toolkit.json"))["devices"]])'
# a GPU pod starts (no StartError)
kubectl get pods -n <ns>
```

## Why UUID over aligning the plugin to index

Both fix the mismatch, but UUID keeps stable device identity (positional index
can mis-map if PCI enumeration reorders), keeps the device plugin at its upstream
default, and fixes the spec at the layer that authors it. Aligning the plugin to
`deviceIDStrategy: index` would work too but weakens identity to positional and
leaves a plugin override to maintain.
