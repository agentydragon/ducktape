# BuildBuddy KVM worker pool rollout

- [Initial worker and VM suite validation](2026_11_03_wyrm2_kvm_worker.md) records provisioning, nested runtimes and the comparison with hosted VM suite executions.
- [Automatic scale-down drain](2026_11_10_kvm_autoscaling_drain.md) follows one busy worker through its last action and deletion.
- [Pool capacity](2026_11_16_kvm_pool_capacity.md) records CPU, memory and placement evidence for the November increase in the worker ceiling.
- [SSD placement on wyrm2 and HDD comparison](2026_11_21_kvm_worker_placement.md) records the placement decision, pinned VM suite timings and next experiment.

See the [pool's shutdown and scaling configuration](../../../../cluster/k8s/buildbuddy-kvm-worker/README.md) and the [experimental Nix image cold-pull investigation](../2026_11_17_nix_image_cold_pulls.md).
