# VM test capacity on the existing KVM pool

- One existing worker under the VM suite. [Worker sizing](vm_kvm_existing_pool_2026_11_12.md), [execution provenance and counters](vm_kvm_existing_pool_2026_11_12.json), [cgroup samples](vm_kvm_existing_pool_2026_11_12_samples.csv)
- [Same-source managed TCG and KVM suite comparison](vm_kvm_required_suite_comparison_2026_11_12.md)
- Queueing on the three-slot pool and completion order in the selected prod-backend test graph. [Burst admission report](vm_kvm_capacity_burst_2026_11_12.md), [physical action evidence](vm_kvm_capacity_burst_2026_11_12.json), [sizing patch](vm_kvm_capacity_burst_2026_11_12.patch)
