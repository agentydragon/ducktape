# NixOS KVM worker qualification

This is a disposable one-member canary in `buildbuddy-executor`, staged at zero
replicas until the managed pull secret is Ready. Its VM, Service,
Cilium endpoint selector, and BuildBuddy pool use `buildbuddy-kvm-worker-nixos-spike`
or `kvm-vm-nixos-spike`. It does not have the production worker's
`app.kubernetes.io/part-of: buildbuddy-kvm-worker` label; the production metrics
Service and KEDA queries therefore exclude it. The canary's VM name also does not
match KEDA's `buildbuddy-kvm-worker-[0-9]+` VMI-name regex.
Its DataVolume template also has a canary-specific name, so generated work PVCs
cannot collide with the production pool's `work-<index>` PVCs.

The manifest is deliberately rendered outside the cluster's GitOps generator: it
is applied and removed as a bounded qualification experiment. The checked-in
KubeVirt shape copies the existing typed `container_disk_pool_template` output;
the package under `x/` cannot import `cluster/cdk8s` per that package's
`AGENTS.md` boundary. `render.py` injects only a pinned containerDisk image and
the runtime ConfigMap. It reads the production guest ConfigMap for the K3s settings and executor config.
Use `--executor-config` to qualify a different executor config. The renderer rejects
mutable image tags and executor configs without runtime API-key substitution.

The guest boots EFI from the NixOS qcow2 containerDisk, attaches a fresh 80-GiB
local SSD work disk, mounts it as ext4 at `/buildbuddy`, and reads the
`buildbuddy-api-key` Secret and this package's runtime ConfigMap as read-only
virtio ISO disks. The ConfigMap has `executor.yaml` from the production worker,
the same K3s registry mirror settings, and a `pool` file containing
`kvm-vm-nixos-spike`. It gives the VM 4 vCPUs (2 requested, 4 limit), 8 GiB RAM,
the same nested-KVM node affinity, and the same 8080 readiness probe with the
`server-type: buildbuddy-executor` header. Readiness is registered executor
health, not merely a running VM. Guest egress permits DNS, the BuildBuddy scheduler, Zot, and NTP to
`time.cloudflare.com` on UDP/123. KubeVirt pulls
the private Forgejo containerDisk with `containerDisk.imagePullSecret`. The
namespace-scoped ExternalSecret reads the existing, controller-owned
`forgejo-images-creds` from `kubernetes-forgejo-images-secret-store`; no token is
copied into the image or manifest. Its local ExternalSecret and target Secret
are named `buildbuddy-kvm-worker-nixos-spike-pull`, keeping canary cleanup
independent of the production pool's Flux-owned `forgejo-images-creds`.

The store must explicitly allow `buildbuddy-executor` before qualification.
[PR #17092](https://github.com/agentydragon/ducktape/pull/17092) provides that
reviewed prerequisite. Reconcile the declarative owner before retrying; do not
copy registry credentials or patch the shared allowlist to bypass that boundary.

The [qualification results](results.md) include the original seven-case run,
the corrected production candidate's retained-disk transition, and in-flight
drain checks. The Linux 6.12 LTS selection is required by the K3s fixture's
legacy iptables support.

From the repo root, with a pushed candidate digest and a task scratch directory:

```bash
scratch=${TMPDIR:-/tmp}/agents/2026_11_16/nixos-kvm-worker-spike
image=git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker-spike@sha256:<64-hex-digest>
bb run //devinfra/buildbuddy/kvm/x/nixos_worker_qualification:render -- \
  --image "$image" \
  --output "$scratch/canary.k8s.yaml"
kubectl apply -f "$scratch/canary.k8s.yaml"
kubectl wait -n buildbuddy-executor --for=condition=Ready \
  externalsecret/buildbuddy-kvm-worker-nixos-spike-pull --timeout=2m
kubectl scale -n buildbuddy-executor \
  vmpool/buildbuddy-kvm-worker-nixos-spike --replicas=1
kubectl wait -n buildbuddy-executor --for=jsonpath='{.status.replicas}'=1 \
  vmpool/buildbuddy-kvm-worker-nixos-spike --timeout=1m
kubectl wait -n buildbuddy-executor --for=jsonpath='{.status.ready}'=true \
  vm -l app.kubernetes.io/name=buildbuddy-kvm-worker-nixos-spike --timeout=15m
```

Before the tests, inspect the VM/VMI and its launcher events. In the guest,
verify `/buildbuddy` is mounted and Docker and the executor are active. Check
the boot journal for ordering cycles and discarded start jobs; `systemctl
--failed` alone does not report jobs removed to break a cycle. Check the
`/readyz` response from the executor with the required header and the
`registered_to_scheduler` metric if the canary metrics Service is scraped.
The real action scheduling check is the smoke test below: it must run on the
new pool and report a KVM API version of 12 plus VM/vCPU creation.

```bash
kubectl get vmpool,vm,vmi,pod -n buildbuddy-executor \
  -l app.kubernetes.io/name=buildbuddy-kvm-worker-nixos-spike -o wide
bbr test //devinfra/buildbuddy/kvm:kvm_vm_nixos_spike_test --nocache_test_results
bbr test //agentplane/system_tests/suites:test_firecracker_nixos_spike --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_nixos_spike --nocache_test_results
```

The three manual targets use the existing KVM smoke, Firecracker, and VM
system-test sources and inputs. The only execution-property change is
`test.Pool=kvm-vm-nixos-spike`; the Firecracker copy is manual so the canary
is never part of required CI. Keep each BuildBuddy invocation URL and target
result with the candidate digest. A locally successful build does not prove
guest boot, BuildBuddy registration, nested KVM, or the real harnesses.

For a drain probe, start the VM system test and wait until its action is running
on the canary. Then scale **this canary pool only** to zero and observe the
test invocation, executor shutdown logs, VM/VMI termination, and work-disk
cleanup. The VM's 87,000-second termination grace and the executor's
`max_shutdown_duration` should allow the owned action to finish; record
whether it actually does. Do not infer graceful drain from an idle shutdown.

```bash
kubectl scale -n buildbuddy-executor vmpool/buildbuddy-kvm-worker-nixos-spike --replicas=0
kubectl get vmpool,vm,vmi,pvc -n buildbuddy-executor
kubectl delete -f "$scratch/canary.k8s.yaml"
```

Confirm no canary VM, VMI, or PVC remains after cleanup. The candidate image
is published by the repository's `nix-images` workflow using its managed
Forgejo credential; this experiment does not create registry credentials.
