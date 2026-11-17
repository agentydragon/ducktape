# VM Sandbox scenarios on the KVM worker

Run [`test_vm.py`](../../suites/test_vm.py) with KubeVirt hardware acceleration on the existing
`kvm-vm` BuildBuddy pool. The target remains `manual` and is not a required CI lane.

An earlier three-scenario suite passed on the existing 4-CPU/8-GiB KubeVirt worker with a 7-GiB
scheduling estimate: 276.857s, a 5.51-GiB action cgroup peak, and no worker OOM or swap use. See the
[measurement and its limits](../../debug/vm_kvm_existing_pool_2026_11_12.md). No separate canary or
worker resize is needed for this measured run; CI routing and fleet concurrency remain separate decisions.
The shared suite now runs the two harness-turn scenarios; the OOM probe has its own manual target.

The earlier wyrm2 trial stayed red and took over ten minutes
([historical record](../../../../devinfra/buildbuddy/debug/2026_11_03_wyrm2_kvm_worker.md)).

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```

## Docker recycling experiment

`test_vm_kvm_recycled` is an opt-in comparison target with the same two scenarios and resource
estimates. It permits the existing fixture to retain K3s, PKI and KubeVirt controllers between
actions while recreating workloads, credentials and workspace storage. Its K3s data lives at
`/var/lib/k3s-recycled`, mounted from a private sparse ext4 file inside the retained Docker container.
K3s's overlay snapshotter cannot use Docker's overlay filesystem directly. Bazel prepares the empty
32-GiB filesystem using its declared e2fsprogs closure and uploads a sparse archive; no bootstrap
package installation is needed. The loop mapping uses AUTOCLEAR and is released after unmount or
container removal. The worker-wide `/var/lib/k3s-env`
mount is unsuitable for a live retained baseline because other actions can reset that directory.

Build both targets normally before a pinned trial:

```bash
bbr build //agentplane/system_tests/x/vm_kvm:test_vm_kvm{,_recycled}
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm_recycled \
  --nocache_test_results --flaky_test_attempts=1
```

The scheduler does not guarantee Docker runner affinity. For a controlled comparison, pass
`--remote_exec_header=x-buildbuddy-platform.debug-executor-id=<current-executor-id>` to each test
invocation. Use the executor registration ID, which changes on restart. This header pins **every**
remote action in that invocation, so prebuild without it and inspect the execution records to
confirm that only the intended test executes. The pin fails if that executor is unavailable.

Compare `recycled-runner.json`, `persistence-suite.json` and `test-spans.jsonl` from successive
uncached actions. Reuse requires the same runner receipt, process and node identities, a warm
fixture outcome and complete cleanup, with new workload and credential identities. A successful
test alone does not prove reuse.

Finish with the same target, executor pin and recycling key plus
`--test_arg=--discard-recycled-runner`. This stops the owned baseline and asks BuildBuddy to remove
the container after uploading outputs. Failures also request disposal. Verify the owned container
has disappeared; a cleanup action scheduled on a different executor cannot dispose of it.
