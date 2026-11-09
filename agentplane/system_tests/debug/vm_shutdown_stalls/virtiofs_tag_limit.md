# QEMU startup failure from an overlength virtiofs tag

The full VM system test failed in [BuildBuddy invocation
`cb469644-f04f-4844-9021-f3a425dce6ef`](https://app.buildbuddy.io/invocation/cb469644-f04f-4844-9021-f3a425dce6ef),
target `//agentplane/system_tests/suites:test_vm`, on effective source `4a2ef6f0`.
Every retained QEMU launch rejected the workload-trust filesystem before guest kernel startup:

```text
qemu-kvm: -device {"driver":"vhost-user-fs-pci", ... "tag":"agentplane-environment-runtime-workload-trust" ...}: tag property must be 36 bytes or less
```

Commit `cdc7bb0c33` introduced the 45-byte UTF-8 tag. The value in
`agentplane/env/images/vm_guest_contract.json` becomes both the KubeVirt filesystem name and the
NixOS mount device. The mount path remains `/run/agentplane/environment-runtime-workload-trust`;
it is not the QEMU tag. The contract and Sandbox Controller now use the short tag
`runner-workload-trust`; the workload CA ConfigMap and guest mount are unchanged. The Debian
diagnostic guest generator uses only the `state` filesystem and does not consume this contract field.

The cdk8s chart carries state and workspace names in the SandboxTemplate; `SandboxController.renderVM`
adds those and the credential filesystems to the KubeVirt filesystem list. The controller rejects
any emitted virtiofs tag longer than 36 UTF-8 bytes before creating the VM, and its contract test
checks both the guest contract and rendered filesystem names, including the injected workload tag.

This is a deterministic launcher startup failure, distinct from the unrelated early-kernel stop
recorded as `46993f63`. This invocation never reached guest kernel boot and provides no evidence
about that earlier failure.
