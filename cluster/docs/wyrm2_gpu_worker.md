# wyrm2 — NixOS GPU Desktop + K8s Worker

wyrm2 is a single NixOS VM on Proxmox that serves as both a K8s worker with GPU access
and a daily-driver Linux desktop.

## Motivation

Proxmox doesn't support elastic RAM allocation across VMs (balloon is unreliable,
virtio-mem not yet available — Bugzilla #2949). Any static RAM split between a GPU
worker and a desktop VM is constraining: the worker is over-provisioned when idle,
the desktop starves during heavy use, or vice versa.

A single VM eliminates the split between desktop and worker. Its Terraform
configuration allocates 96 GiB to wyrm2 and leaves the remainder to the host.

| VM    | Role                     | RAM    | GPUs    | OS    |
| ----- | ------------------------ | ------ | ------- | ----- |
| wyrm2 | K8s GPU worker + desktop | 96 GiB | 2x 5090 | NixOS |

## GPU Mode Switching

The GPUs are always passed through to the single VM via VFIO. The question is whether
kubelet is claiming them for cluster workloads or they're free for desktop/gaming use.

### Light switch: taint toggle (no kubelet restart)

Keeps the node in the cluster. A `NoExecute` taint evicts **every** pod on the
node that lacks the matching toleration, including non-GPU workloads. First inspect
all pods and tolerations, prove disruption and placement safety, and get approval
for the exact eviction. A drain similarly affects the node's other workloads.

```bash
# "Gaming mode" — evict GPU workloads
kubectl taint nodes wyrm2 nvidia.com/gpu=true:NoExecute --overwrite

# "Cluster mode" — allow GPU workloads
kubectl taint nodes wyrm2 nvidia.com/gpu:NoExecute-
kubectl taint nodes wyrm2 nvidia.com/gpu=true:PreferNoSchedule --overwrite
```

GPU-consuming pods (Ollama) must tolerate `PreferNoSchedule` but not `NoExecute`.
The two effects are distinct taints: adding `PreferNoSchedule` does not remove
`NoExecute`. Once the latter is removed, the controller can schedule workloads again.

### Heavy switch: drain + stop kubelet

For full GPU release (no device plugin, no container runtime holding GPU contexts):

```bash
# Enter gaming mode
kubectl drain wyrm2 --ignore-daemonsets --delete-emptydir-data
sudo systemctl stop kubelet

# Exit gaming mode
sudo systemctl start kubelet
kubectl uncordon wyrm2
```

### Which to use?

- **Taint toggle**: Keeps the node in the cluster, but evicts all pods that do not
  tolerate the taint. Check device-plugin and other workload behavior before use.
- **Drain + stop**: Full cleanup. Use if something holds a GPU context that interferes
  with gaming (e.g., a CUDA process that didn't exit cleanly).

A desktop script/shortcut can automate either workflow.

## NixOS Configuration

Built on the `k8s-worker` NixOS module (`nix/nixos/modules/k8s-worker.nix`) with
`enableNvidiaRuntime = true`:

- **NVIDIA drivers**: `hardware.nvidia` with open kernel modules
- **CDI spec generation**: `hardware.nvidia-container-toolkit` generates CDI specs at
  `/var/run/cdi/` on boot (maps NixOS nix-store paths to FHS paths inside containers)
- **Containerd runtime**: `nvidia-container-runtime.cdi` registered as a named runtime
  (`pkgs.nvidia-container-toolkit.tools`), with CDI enabled and spec dirs configured
- **RuntimeClass**: `nvidia` in `cluster/cdk8s/nvidia_runtimeclass.py` maps to the containerd runtime
- **Device plugin**: NVIDIA device plugin Helm chart with `runtimeClassName: nvidia` and
  default `envvar` device list strategy (no internal CDI spec generation — avoids
  NixOS FHS path incompatibility in `tryResolveLibrary`)
- **Workload pods**: Must specify `runtimeClassName: nvidia` to get GPU access

## Open Questions

- **Partial GPU detach**: Could expose only 1 GPU to k8s (via device plugin config
  file `NVIDIA_VISIBLE_DEVICES` filter) and keep 1 for desktop. Adds complexity;
  probably not worth it vs the clean taint-toggle approach.

## Related

- <roaming_laptop_worker.md> — NixOS k8s-worker module design and testing
- Cluster plan entries: <plan.md> § "Future Directions" (GPU: DRA; GPU: KubeRay, Kueue)
