# VM Sandbox scenarios on the KVM worker

An experiment: run [`test_vm.py`](../../suites/test_vm.py) with KubeVirt hardware acceleration on the
`kvm-vm` BuildBuddy pool instead of on the software-emulated stack. It carries the `manual` tag and
nothing runs it.

The wyrm2 trial ran all three scenarios with real KVM, but the target stayed red and took over ten minutes;
the lane was not adopted ([record](../../../../devinfra/buildbuddy/debug/2026_11_03_wyrm2_kvm_worker.md)).
The worker pool is sized for [`test_firecracker`](../../suites/test_firecracker.py), not for this suite, which
has not been measured and may need a bigger executor.

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```
