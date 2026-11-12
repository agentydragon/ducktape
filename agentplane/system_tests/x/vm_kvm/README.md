# VM Sandbox scenarios on the KVM worker

An experiment: run [`test_vm.py`](../../suites/test_vm.py) with KubeVirt hardware acceleration on the
`kvm-vm` BuildBuddy pool instead of on the software-emulated stack. It carries the `manual` tag and
nothing runs it.

The wyrm2 trial ran all three scenarios with real KVM, but the target stayed red and took over ten minutes;
the lane was not adopted ([record](../../../../devinfra/buildbuddy/debug/2026_11_03_wyrm2_kvm_worker.md)).
The production worker pool is sized for [`test_firecracker`](../../suites/test_firecracker.py): each
worker advertises 7 GiB, below this suite's 10-GB request. The original `test_vm_kvm` target
cannot be placed there. The separate [manual VM pilot](../../../../devinfra/buildbuddy/kvm/x/vm_pilot/README.md)
provides an opt-in 16-GiB guest with a 15-GiB executor and keeps the same test request.

After starting the manual pilot, route only this suite to its unique pool:

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm_canary --nocache_test_results
```
