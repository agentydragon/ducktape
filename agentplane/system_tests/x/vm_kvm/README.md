# VM Sandbox scenarios on the KVM worker

Run [`test_vm.py`](../../suites/test_vm.py) with KubeVirt hardware acceleration on the existing
`kvm-vm` BuildBuddy pool. The required target is `//agentplane/system_tests/suites:test_vm`. This package holds the
manual cold-worker, alternate-pool and cleanup probes. See [CI executor routing](../../../../devinfra/ci/README.md#where-bazel-ci-runs-tests).

An earlier three-scenario suite passed on the existing 4-CPU/8-GiB KubeVirt worker with a 7-GiB
scheduling estimate: 276.857s, a 5.51-GiB action cgroup peak, and no worker OOM or swap use. See the
[measurement and its limits](../../debug/vm_kvm_capacity/vm_kvm_existing_pool_2026_11_12.md). No separate canary or
worker resize is needed for this measured run; CI routing and fleet concurrency remain separate decisions.
The shared suite now runs the two harness-turn scenarios; the OOM probe has its own manual target.

The earlier wyrm2 trial stayed red and took over ten minutes
([historical record](../../../../devinfra/buildbuddy/debug/kvm_pool_rollout/2026_11_03_wyrm2_kvm_worker.md)).

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```

## Docker recycling experiment

The [17 November qualification](../../debug/kvm_docker_recycling.md) initially rejected retention
under the executor's 2-GB default. A higher-cap follow-up passed two full warm actions, an ordinary
same-worker action alongside the paused baseline, and explicit disposal. See the linked CI reports for the subsequent paired measurements.

The required `test_vm` target runs those same two scenarios and permits the existing fixture to retain K3s, PKI and KubeVirt controllers between
actions while recreating workloads, credentials and workspace storage. Its K3s data lives at
`/var/lib/k3s-recycled`, mounted from a private sparse ext4 file inside the retained Docker container.
K3s's overlay snapshotter cannot use Docker's overlay filesystem directly. Bazel prepares the empty
32-GiB filesystem using its declared e2fsprogs closure and uploads a sparse archive; no bootstrap
package installation is needed. The loop mapping uses AUTOCLEAR and is released after unmount or
container removal. The worker-wide `/var/lib/k3s-env`
mount is unsuitable for a live retained baseline because other actions can reset that directory.

The pinned executor subtracts a cgroup-v1 cache field from Docker's memory usage. On these cgroup-v2
workers it consequently counts file cache against retention. The [worker config](../../../../devinfra/buildbuddy/kvm/worker/executor.yaml)
sets 10,000,000,000 bytes; the pinned executor clamps this to its advertised allocation, currently
7 GiB. This does not increase guest RAM or active-task capacity. Existing executor processes need a
restart to load the pool limit. After successful fixture
cleanup the probe records cgroup memory, asks the kernel to reclaim file cache in this runner's private
cgroup (`memory.reclaim`, `swappiness=0`), and records the result. The pool's limits remain authoritative;
reclamation can be partial, and the executor can still discard a runner that exceeds its cap. The
receipt also preserves OOM counters. See the [kernel interface](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files).

Build both targets normally before a pinned trial:

```bash
bbr build //agentplane/system_tests/x/vm_kvm:test_vm_kvm //agentplane/system_tests/suites:test_vm
bbr test //agentplane/system_tests/suites:test_vm \
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

## Retained failure-cleanup qualification

`test_retained_failure_cleanup` injects one sentinel after a real Sandbox reaches Ready with a
KubeVirt VM using a raw loop-backed volume. It records the merged retained-baseline guard entry,
owned K3s/container processes, actual open FDs for the VM's block device, and the final sysfs loop
mapping. The probe writes its detailed receipt to undeclared outputs and requests disposal of its
recycled runner on failure.

Run the manual qualification with:

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_retained_failure_cleanup \
  --nocache_test_results --test_output=errors --flaky_test_attempts=1 --runs_per_test=1 \
  --test_arg=--discard-recycled-runner
```

The first 17 November 2026 live run was negative: the retained-stack guard stopped the owned K3s
process, container tasks, and VM process holding the raw device, but a Kubelet loop lease kept
`/dev/loop1` attached. Teardown raised an `ExceptionGroup` instead of preserving the injected
sentinel. After the detach path learned to release only kernel loop mappings that identify its
exact owned block device, the live RBE qualification passed: the original sentinel was preserved,
the Kubelet lease disappeared, and the owned loop detached. The receipt also verifies that the
diagnostic recycled runner was discarded.
