# wyrm2 — NixOS GPU Desktop + K8s Worker

wyrm2 is a single NixOS VM on Proxmox that serves as both a K8s worker with GPU access
and a daily-driver Linux desktop.

## Motivation

Proxmox doesn't support elastic RAM allocation across VMs (balloon is unreliable,
virtio-mem not yet available — Bugzilla #2949). Any static RAM split between a GPU
worker and a desktop VM is constraining: the worker is over-provisioned when idle,
the desktop starves during heavy use, or vice versa.

A single VM eliminates the split entirely — one VM gets ~28GB (leaving ~4GB for
Proxmox host + lightweight Talos CP VM).

| VM    | Role                     | RAM   | GPUs    | OS    |
| ----- | ------------------------ | ----- | ------- | ----- |
| wyrm2 | K8s GPU worker + desktop | ~28GB | 2x 5090 | NixOS |

The Talos control plane VM on Proxmox stays unchanged (lightweight, ~4GB).

## GPU Mode Switching

The GPUs are always passed through to the single VM via VFIO. The question is whether
kubelet is claiming them for cluster workloads or they're free for desktop/gaming use.

### Light switch: taint toggle (no kubelet restart)

Keeps the node in the cluster. DaemonSets (Cilium, Promtail, etc.) keep running.
Only GPU-consuming pods are affected.

```bash
# "Gaming mode" — evict GPU workloads
kubectl taint nodes gpu-node nvidia.com/gpu=true:NoExecute --overwrite

# "Cluster mode" — allow GPU workloads
kubectl taint nodes gpu-node nvidia.com/gpu=true:PreferNoSchedule --overwrite
```

GPU-consuming pods (Ollama) must tolerate `PreferNoSchedule` but not `NoExecute`.
When the taint flips to `NoExecute`, the scheduler evicts them. When it flips back,
they reschedule.

### Heavy switch: drain + stop kubelet

For full GPU release (no device plugin, no container runtime holding GPU contexts):

```bash
# Enter gaming mode
kubectl drain gpu-node --ignore-daemonsets --delete-emptydir-data
sudo systemctl stop kubelet

# Exit gaming mode
sudo systemctl start kubelet
kubectl uncordon gpu-node
```

### Which to use?

- **Taint toggle**: Fast (~seconds), keeps node healthy in cluster, sufficient when
  the device plugin doesn't hold GPU memory (it doesn't — it only advertises the
  resource). Good enough for most cases.
- **Drain + stop**: Full cleanup. Use if something holds a GPU context that interferes
  with gaming (e.g., a CUDA process that didn't exit cleanly).

A desktop script/shortcut can automate either workflow.

## NixOS Configuration

Built on the `k8s-worker` NixOS module (`nix/nixos/modules/k8s-worker.nix`) with
`enableNvidiaRuntime = true`:

- **NVIDIA drivers**: `hardware.nvidia` with open kernel modules (driver 580.119.02)
- **CDI spec generation**: `hardware.nvidia-container-toolkit` generates CDI specs at
  `/var/run/cdi/` on boot (maps NixOS nix-store paths to FHS paths inside containers)
- **Containerd runtime**: `nvidia-container-runtime.cdi` registered as a named runtime
  (`pkgs.nvidia-container-toolkit.tools`), with CDI enabled and spec dirs configured
- **RuntimeClass**: `nvidia` RuntimeClass in `helmrelease.yaml` maps to the containerd runtime
- **Device plugin**: NVIDIA device plugin Helm chart with `runtimeClassName: nvidia` and
  default `envvar` device list strategy (no internal CDI spec generation — avoids
  NixOS FHS path incompatibility in `tryResolveLibrary`)
- **Workload pods**: Must specify `runtimeClassName: nvidia` to get GPU access

## Open Questions

- **Partial GPU detach**: Could expose only 1 GPU to k8s (via device plugin config
  file `NVIDIA_VISIBLE_DEVICES` filter) and keep 1 for desktop. Adds complexity;
  probably not worth it vs the clean taint-toggle approach.

## Related

- <roaming-laptop-worker.md> — NixOS k8s-worker module design and testing
- Cluster plan entry: `docs/plan.md` "GPU Worker Node"
