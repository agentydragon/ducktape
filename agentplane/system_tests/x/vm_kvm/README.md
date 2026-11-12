# VM Sandbox scenarios on the KVM worker

Run [`test_vm.py`](../../suites/test_vm.py) with KubeVirt hardware acceleration on the existing
`kvm-vm` BuildBuddy pool. The target remains `manual` and is not a required CI lane.

The unchanged three-scenario suite passed on the existing 4-CPU/8-GiB KubeVirt worker with a 7-GiB
scheduling estimate: 276.857s, a 5.51-GiB action cgroup peak, and no worker OOM or swap use. See the
[measurement and its limits](../../debug/vm_kvm_existing_pool_2026_11_12.md). No separate canary or
worker resize is needed for this measured run; CI routing and fleet concurrency remain separate decisions.

The earlier wyrm2 trial stayed red and took over ten minutes
([historical record](../../../../devinfra/buildbuddy/debug/2026_11_03_wyrm2_kvm_worker.md)).

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```
