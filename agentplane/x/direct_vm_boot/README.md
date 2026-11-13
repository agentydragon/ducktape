# Direct guest boot contract

This manual experiment boots the current VM fixture's kernel, initramfs, root
filesystem and Runtime disk with the pinned KubeVirt launcher image's QEMU and
virtiofsd. It runs through Docker on managed RBE, using software emulation rather
than KVM. No K3s cluster or service-image stack is started.

```sh
bbr test //agentplane/x/direct_vm_boot:test_guest_boot --nocache_test_results
```

A test-only root overlay adds one systemd service. The guest verifies systemd as
PID 1, cgroup v2, the real virtiofs state mount, a newly formatted workspace, the
three serial-addressed configuration ISOs and the Runtime EROFS mount. It checks
that the Runtime executable exists, writes a fresh host-chosen nonce through
virtiofs, then powers off. The host requires the marker, all seven mount records,
QEMU success and Linux's power-down message. Serial, tool and cleanup logs go to
Bazel undeclared outputs.

The configuration ISOs are empty. The Runtime is not configured or required to
be healthy: this is a kernel/systemd/mount/packaging contract. It does not establish
controller reconciliation, issued credentials, KubeVirt behavior, authenticated
Runtime requests, harness turns, egress or Kubernetes storage. Existing deployment
acceptance remains required.

The opt-in control changes the required configuration disk's serial. It boots
Linux and systemd but must fail the same guest-readiness assertion because the
real configuration mount cannot appear:

```sh
bbr test //agentplane/x/direct_vm_boot:test_guest_boot --nocache_test_results \
  --test_env=DIRECT_VM_BOOT_MISSING_CONFIG_CONTROL=1
```

The control has its own shorter bound. A failed control is expected evidence;
ordinary runs execute only the healthy guest.
