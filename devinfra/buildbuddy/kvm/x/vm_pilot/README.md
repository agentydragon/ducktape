# Manual KVM VM-suite pilot

The existing `kvm-vm` pool advertises 7 GiB per worker. The experimental VM
system test requests 10 GB, so BuildBuddy cannot place it there. This renderer
uses the committed worker artifact to create one separately named, initially
halted VM in pool `kvm-canary-20261109`. It reserves four CPUs and 16 GiB for
the guest, advertises four CPUs and 15 GiB to BuildBuddy, and retains one action
slot. The test's 10-GB request remains unchanged. The 80-GiB work disk uses the
same disposable SSD class as the production pool.

The renderer emits a ConfigMap, VM, network policy, metrics Service and
ServiceMonitor. It does not emit a KEDA scaler or Flux Kustomization. Its
selectors and BuildBuddy pool name are separate from the production pool; its
pod anti-affinity still avoids a node already hosting a production worker.
The worker uses the existing BuildBuddy API-key Secret. Run only trusted test
code on this worker.

Render and validate from the repository root:

```bash
bb run //devinfra/buildbuddy/kvm/x/vm_pilot:render_bin -- \
  --worker-manifest "$PWD/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml" \
  --output "$PILOT_SCRATCH/worker.yaml"
kubectl create --dry-run=server -f "$PILOT_SCRATCH/worker.yaml"
```

Before creating it, check the current HIL nodes for four CPU, 16-GiB memory
and an 80-GiB local SSD allocation plus operational headroom. The selected
host may differ from earlier canary measurements. The VM starts Halted; after
reviewing the rendered objects and capacity, create only these five objects,
start the named VM, and wait until its `/readyz` reports registration. Use the
same code revision for the smoke check and the VM suite:

```bash
kubectl create -f "$PILOT_SCRATCH/worker.yaml"
virtctl -n buildbuddy-executor start kvm-canary-20261109
bbr test //devinfra/buildbuddy/kvm:kvm_vm_pilot_test --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm_canary --nocache_test_results
```

The VM suite remains a manual experiment. The smoke check verifies nested KVM
and action permissions; neither test is a rollout gate for the production
pool. Record BuildBuddy action IDs and results, the guest and node identity,
and phase timings. The 16-GiB allocation establishes scheduling eligibility;
it does not establish that the suite will pass or that this is the right
steady-state pool size.

Keep this canary the sole producer of its unique pool. Before shutdown,
reconcile every submitted invocation and physical execution, and verify no
action container, K3s process or mount remains. Stop the guest's
`buildbuddy-executor.service` and verify its graceful shutdown before stopping
the VM. Delete only the five named pilot objects after the VMI and launcher
pod disappear. Inspect the pilot VM's DataVolume, PVC, PV and LINSTOR ownership
and verify that its disposable disk is removed; never delete a volume by a
name alone. Confirm the production worker remains registered and Ready.
