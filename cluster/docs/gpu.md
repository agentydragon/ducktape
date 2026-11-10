# GPU (NVIDIA) on wyrm2

See [wyrm2's NixOS GPU configuration](wyrm2_gpu_worker.md#nixos-configuration)
for the CDI/containerd/device-plugin stack. GPU workloads require `runtimeClassName: nvidia`.

Source and workload entry points:

- [k8s-worker.nix](../../nix/nixos/modules/k8s-worker.nix) — containerd runtime and CDI settings
- [nvidia_device_plugin.py](../cdk8s/nvidia_device_plugin.py) — device plugin
- [nvidia_runtimeclass.py](../cdk8s/nvidia_runtimeclass.py) — RuntimeClass
- [Ollama workload](../cdk8s/ollama/app.py) — GPU pod example
