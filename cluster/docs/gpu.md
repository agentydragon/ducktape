# GPU (NVIDIA) on wyrm2

wyrm2 is a NixOS machine (not Talos) joined as a K8s worker via `k8s-worker.nix` and
Nebula mesh. It provides 2x RTX 5090 GPUs to the cluster.

**Stack**: NixOS `hardware.nvidia-container-toolkit` generates CDI specs at
`/var/run/cdi/` → containerd configured with `nvidia-container-runtime.cdi` as a named
runtime → `RuntimeClass` resource maps `nvidia` handler to that runtime → NVIDIA device
plugin (Helm chart) discovers GPUs via NVML and advertises `nvidia.com/gpu` resources.

**How it works**: The device plugin uses the default `envvar` strategy — it sets
`NVIDIA_VISIBLE_DEVICES` on workload containers. Pods requesting GPUs must specify
`runtimeClassName: nvidia` so containerd routes them through `nvidia-container-runtime.cdi`,
which reads the env var and injects GPU devices/libraries via host CDI specs.

**Key files**:

- `nix/nixos/modules/k8s-worker.nix` — containerd nvidia runtime config, CDI settings
- `cluster/k8s/nvidia-device-plugin/helmrelease.yaml` — device plugin + RuntimeClass
- `cluster/k8s/ollama/deployment.yaml` — example GPU workload (`runtimeClassName: nvidia`)
