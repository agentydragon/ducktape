# Direct guest contracts

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

## Authenticated packaged Runtime

```sh
bbr test //agentplane/x/direct_vm_boot:test_runtime_gateway --nocache_test_results
```

This separate manual target boots the unmodified fixture guest root and Runtime disk,
with configuration, launch identity and fixture-issued TLS credentials. The real
Gateway reaches the Runtime through QEMU's loopback port forward. The assertion
requires authenticated status, a fresh boot ID, running lifecycle and a Gateway
Lease owned by the exact Sandbox with the expected Pod UID and connection epoch.
The fixture assigns the Pod address before Gateway starts observing it.

State belongs to this case. Its exported ownership matches the guest's 107:107
home-setup identity; launcher cleanup restores host ownership, stops QEMU and
virtiofsd, removes the named container and checks that the listener is gone.
This target does not assert orderly guest poweroff; the boot-only target above
owns that behavior.

No Session or supervised executor is created. The ingress endpoint is configured
but unused, so this target makes no History-delivery, harness, executor or cgroup
operation claim. Envtest authors Sandbox/Pod status, and a test CA supplies trust.
The test does not establish controller reconciliation, cert-manager issuance,
Secret projection, KubeVirt, PVC or App behavior. Those deployment cases remain.

The client-trust control supplies an unrelated CA to the Runtime's Gateway-client
verification. It retains the healthy authenticated-readiness assertion and must
fail it after the packaged Runtime starts:

```sh
bbr test //agentplane/x/direct_vm_boot:test_runtime_gateway --nocache_test_results \
  --runs_per_test=1 --flaky_test_attempts=1 \
  --test_env=DIRECT_VM_BOOT_WRONG_GATEWAY_CLIENT_CA_CONTROL=1
```
