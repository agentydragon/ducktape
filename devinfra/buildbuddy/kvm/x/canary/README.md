# Single-slot KVM canary

This opt-in experiment renders one manually owned worker, initially stopped.
It uses the committed production artifact without changing its Flux inventory.
The unique VM, ConfigMap, network policy and BuildBuddy pool are
`kvm-canary-20261109`. The guest has 16 GiB RAM, 80 GiB disposable HDD scratch,
one action slot, and either four or eight vCPUs. The guest adds `sysstat` for
`pidstat` and `iostat`; application images and fixture inputs stay unchanged.

Before provisioning, verify that the identity is absent, the selected node can
reserve the guest **in addition to** existing workloads, the host filesystem can
accommodate the full scratch allocation, and nested KVM is enabled. Record actual
host CPU, memory and I/O pressure; scheduler request headroom alone does not show
contention. The same-host SSD arm requires separate physical capacity verification.
The renderer intentionally offers only the existing HDD path.

Render and review an arm from the repository root:

```bash
bb run //devinfra/buildbuddy/kvm/x/canary:render_bin -- \
  --worker-manifest "$PWD/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml" \
  --node wyrm2 --cpu 4 --output "$CANARY_SCRATCH/cpu4.yaml"
kubectl create --dry-run=server -f "$CANARY_SCRATCH/cpu4.yaml"
```

After reviewing the manifest and capacity, create it and start only this VM:

```bash
kubectl create -f "$CANARY_SCRATCH/cpu4.yaml"
virtctl -n buildbuddy-executor start kvm-canary-20261109
virtctl -n buildbuddy-executor console kvm-canary-20261109
```

Verify successful bootstrap, `/dev/kvm`, nested KVM and the unique pool's idle
registration before submitting one action. Run both arms from the same commit:

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
completed, the canary has no queued/active reservations, no action container or
K3s stack remains, and the guest is idle. Then stop this VM and wait for its VMI
and launcher Pod to disappear. Render the other CPU arm and update only the
owned objects while halted; never run two canaries simultaneously. Guest package
installation is not part of the measured action but record installed versions.

For final cleanup, repeat the drain/idle checks, stop the canary, and delete its
three owned objects by exact name. Verify their absence, the disappearance of
the canary executor, and production worker identity/readiness. No PVC, existing
worker, shared cache, storage pool, KEDA setting or node configuration is changed.
