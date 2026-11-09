# Single-slot KVM canary

This opt-in experiment renders one manually owned worker, initially stopped.
It uses the committed production artifact without changing its Flux inventory.
The unique VM, ConfigMap, network policy and BuildBuddy pool are
`kvm-canary-20261109`. The guest has 16 GiB RAM, 80 GiB disposable scratch,
one action slot, and either four or eight vCPUs. The guest adds `sysstat` for
`pidstat` and `iostat`; application images and fixture inputs stay unchanged.

The VM selects zone `hil-ovh` with no hostname constraint. Kubernetes chooses an
eligible HIL host from CPU/RAM, KVM capability and an explicit 80 GiB
`ephemeral-storage` request. Before provisioning, verify that all currently
feasible destinations have the full scratch allocation plus operational reserve
on the actual kubelet filesystem. Record actual host CPU, memory and I/O pressure;
scheduler request headroom alone does not show contention. Record the scheduled
node and scratch backend for every arm. Comparisons across different hosts are
confounded; preserve unpinned placement and stratify results by actual host.

Render and review an arm from the repository root:

```bash
bb run //devinfra/buildbuddy/kvm/x/canary:render_bin -- \
  --worker-manifest "$PWD/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml" \
  --cpu 4 --output "$CANARY_SCRATCH/cpu4.yaml"
kubectl create --dry-run=server -f "$CANARY_SCRATCH/cpu4.yaml"
```

After reviewing the manifest and capacity, create it and start only this VM:

```bash
kubectl create -f "$CANARY_SCRATCH/cpu4.yaml"
virtctl -n buildbuddy-executor start kvm-canary-20261109
virtctl -n buildbuddy-executor console kvm-canary-20261109
```

Verify the scheduled host's health and scratch backing, successful bootstrap,
`/dev/kvm`, nested KVM and the executor's unique pool before submitting one action.
CPU-feature labels and `kvm_intel nested=Y` are eligibility checks; prove actual
nested guest execution with the first owned KVM workload. Run both arms from the
same commit:

```bash
bbr test //agentplane/system_tests/suites:test_firecracker \
  --define=kvm-canary-20261109=4 --nocache_test_results
```

The define changes only KVM suites' TestRunner properties: the pool, explicit
CPU request (four or eight), and fixed 10 GB action memory request. The VM and
advertised executor CPU must match the selected arm. Explicit CPU and memory
override compute units in BuildBuddy; changing advertised worker CPU alone would
leave the action request at four. The pinned Docker backend supplies no action
CPU quota, so the outer VM provides the actual four/eight CPU boundary.

Capture existing phase/OTel outputs, BuildBuddy execution timestamps and input
identity, guest `pidstat -durh -p ALL 1`, `iostat -xz 1`, host pressure, and the
action Docker cgroup's `cpu.stat`, `memory.peak`, `memory.events` and `io.stat`.
Do not export Docker environment/credential files. A sampled RSS maximum is not
the cgroup peak. These observations distinguish phase wall time, process CPU,
memory peaks and shared-host contention. CPU availability does not imply usage.

Start with three serial pairs; classify recreated guests as cold and repeat
within an arm for warm behavior. A stopped VM's `emptyDisk` is discarded.
Before changing CPU, retain outputs and prove that the owned BuildBuddy demand
completed, no action container or K3s stack remains, and the guest is idle. Then
stop executor admission and verify graceful termination before stopping the VM.
Wait for its VMI and launcher Pod to disappear. Render the other CPU arm and update only the
owned objects while halted; never run two canaries simultaneously. Guest package
installation is not part of the measured action but record installed versions.

For final cleanup, repeat the drain/idle checks, stop the canary, and delete its
three owned objects by exact name. Verify their absence, the disappearance of
the canary executor, and production worker identity/readiness. No PVC, existing
worker, shared cache, storage pool, KEDA setting or node configuration is changed.

## Drain without an admin API

Keep this fresh opt-in pool a sole-producer experiment: one locally recorded
invocation at a time, no external workflow selection. Record every parent/child
invocation and execution ID, worker identity, scheduled node and test result.
Before stopping the guest, reconcile all owned invocations to terminal status
and match each physical execution to the executor's action-completion logs.
Terminal invocation state alone does not prove that its action stopped.

From the administrative guest console, retain the executor journal privately and
inspect `docker ps -a` using only ID/name/state/image fields. The only live Docker
container must be `buildbuddy-kvm-worker-executor`; inspect action cgroup counters
before their containers are removed. Verify no K3s process, nested containerd
process or mount remains beneath `/buildbuddy/k3s` (`/var/lib/k3s-env` in actions).
Resolve any pending or unmatched execution before teardown.

After those idle checks, run `systemctl stop buildbuddy-executor` in this canary
guest. Require a successful systemd result, executor shutdown/completion logs,
no live executor process/container, and no action/K3s process or mount. The service
stop disables its automatic restart until explicitly started. Retain the last
action completion and executor-stop journal as the drain receipt, then stop the
owned VM and wait for its VMI/launcher Pod to disappear. Do not read a stronger
BuildBuddy credential to obtain queue visibility.
