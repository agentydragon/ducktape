# Static KVM worker

One always-running KubeVirt VM on `wyrm2` contains the BuildBuddy executor for the manual
`wyrm2-kvm-vm` pool. It boots KubeVirt's digest-pinned Ubuntu 24.04 cloud image, with eight
vCPUs, 24 GiB RAM and an 80 GiB `emptyDisk` for Docker layers, action workspaces and cache.
Cloud-init installs Ubuntu's Docker package and the native
[worker configuration](../../../devinfra/buildbuddy/kvm/worker/). There is no custom guest
image build. Docker package installation uses the Ubuntu repositories at first boot.
Both Docker's data root and containerd's image store use the work disk; Docker 29's
containerd image store does not follow Docker's `data-root` setting.
Actions also bind K3s's data directory from the ext4 work disk so its nested overlayfs
snapshotter has a supported backing filesystem.
The guest sets kubelet's kernel defaults at boot. The KVM fixture prepares its private
Docker cgroup and network namespaces; guest-wide sysctls remain read-only in the action.
K3s pulls its bundled images through Zot, with public registry fallback disabled, using
read-only configuration supplied by the worker.

The existing `buildbuddy-api-key` Secret is attached as a read-only ISO. No outer Kubernetes
token or host filesystem is attached. Root in a test can compromise the persistent guest
and its BuildBuddy key; this is a trusted-code worker.

The Kustomization is enabled, so Flux deploys the VM when this configuration lands.
First boot configures the dedicated guest for nested KVM and reboots once before the
executor starts. Verify guest boot, the outer network fence and these manual checks:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/suites:test_vm_kvm --nocache_test_results
```

The outer policy denies ingress and admits DNS, BuildBuddy and its registry, Ubuntu package
repositories over TLS, and Zot. Use
`virtctl -n buildbuddy-executor console buildbuddy-kvm-worker` for diagnostics. Updating the
VM template requires `virtctl -n buildbuddy-executor restart buildbuddy-kvm-worker`; this
recreates its root and work disk, discarding the cache. Rotate the key too if the guest was compromised.
