# Static KVM worker

One always-running KubeVirt VM on `wyrm2` contains the BuildBuddy executor for the manual
`wyrm2-kvm-vm` pool. It has eight vCPUs, 24 GiB RAM, an ephemeral root and an 80 GiB
`emptyDisk` for Docker layers, action workspaces and cache. The existing `buildbuddy-api-key`
Secret is attached as a read-only ISO. No outer Kubernetes token or host filesystem is
attached to the guest. Root in a test can compromise the persistent guest and its BuildBuddy
key; this is a trusted-code worker.

Flux initially leaves this Kustomization suspended. Publish the
[worker image](../../../devinfra/buildbuddy/images/kvm_worker/README.md), select its `devel-*`
tag in `image-pins/kustomization.yaml`, and change `suspend` in
[`buildbuddy_kvm_worker.py`](../../cdk8s/buildbuddy_kvm_worker.py). Regenerate the manifests,
commit and review that activation through Git. The manual checks are:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/suites:test_vm_kvm --nocache_test_results
```

The outer policy admits DNS, BuildBuddy and its executor registry over TLS, and Zot; ingress
is denied. Guest boot, network enforcement and nested KVM still need live verification.
Use `virtctl -n buildbuddy-executor console buildbuddy-kvm-worker` for diagnostics.
Image updates change the VM template; apply them with
`virtctl -n buildbuddy-executor restart buildbuddy-kvm-worker`. Restarting the VMI recreates
its root and work disk, discarding the worker cache. Rotate the key too if the guest was compromised.
