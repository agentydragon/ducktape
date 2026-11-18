# Firecracker fixture-lifetime benchmark

These `manual`, `bench` targets run the existing two Firecracker scenarios on `kvm-vm`.
Both use the same private ext4 K3s directory, receipt plugin and 4-CPU/6-GB/20-GB scheduling
estimates. The control always discards its container; the candidate permits Docker recycling.
The required suite uses the same retained lifecycle; its cold-start acceptance target is
scheduled separately. Scenario assertions are shared with both benchmarks.

The shared [Docker runner plugin](../../k3s/recycling_probe.py) owns the private data disk and disposal.
K3s and PKI controllers may survive; workloads, credentials, storage and the KVM device-plugin
namespace are recreated. `firecracker-runtime.json` records the plugin identities and namespace
removal before the baseline is certified clean. `persistence-suite.json` and
`recycled-runner.json` establish baseline and action identity, cleanup and cgroup memory.

Prebuild without an executor pin, then run explicit labels without `--config=ci`, which excludes
manual targets:

```bash
bbr build //agentplane/system_tests/x/firecracker:test_firecracker_{control,recycled} \
  //agentplane/system_tests/x/vm_kvm:test_vm_kvm
bbr test //agentplane/system_tests/x/firecracker:test_firecracker_control \
  --nocache_test_results --flaky_test_attempts=1
bbr test //agentplane/system_tests/x/firecracker:test_firecracker_recycled \
  --nocache_test_results --flaky_test_attempts=1
```

For a controlled warm repeat, use the cold action's actual executor registration ID with
`--remote_exec_header=x-buildbuddy-platform.debug-executor-id=<id>`. The pin affects every
remote action, so prebuild first and verify native records. This proves controlled reuse, not
ordinary scheduler affinity. Check exact previous-action linkage, stable K3s/process/node/PKI
identities, fresh fixture and device-plugin identities, complete cleanup and memory/OOM evidence.
A passing action alone does not prove reuse.

Finish on that same executor/key with the candidate and
`--test_arg=--collect-only --test_arg=--discard-recycled-runner`. This runs no scenario bodies;
keep it outside latency comparisons. Verify its exact receipt chain and the disappearance of
the owned Docker container and private loop/backing identity from the original worker kernel.
Failures request disposal too. Never dispose of another runner using an unrelated receipt.
