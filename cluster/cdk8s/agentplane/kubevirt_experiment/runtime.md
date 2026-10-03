# Runner guest experiment

Guest definition: [`agentplane/runner/vm.nix`](../../../../agentplane/runner/vm.nix).
Boot seed and native-process environment:
[`guest_config.py`](../../../../agentplane/runner/guest_config.py).
Launcher admission and networking: [platform experiment](README.md).

## Build

The [VM image workflow](../../../../.github/workflows/agentplane-runner-vm-image.yml)
builds the current runner wheel and supplies `DUCKTAPE_ARTIFACT_OVERRIDES` to
`.#agentplane-runner-vm-container-disk`. Without that override, a local Nix build
uses the repository's pinned wheel, which may predate the guest entry points.
Normal registry publication is restricted to `devel`; experiments use a separate
tag and a digest-pinned containerDisk.

## Required VM inputs

Attach the public config disk with serial `agentplane-config`, the trust-manager
ConfigMap disk with serial `agentplane-trust`, and fresh state/workspace disks.
The config file names, validation, mount paths, and filesystem labels are defined
in the guest code linked above.

The runner VM wiring is in [`runner_vm`](vm.py#L119); the [runtime acceptance helper](runtime_acceptance.md)
drives it through direct KubeVirt APIs and runs the guest RPC, recovery, and setup probes.

The trust ConfigMap must be in the VM namespace and contain trust-manager's
`ca-certificates.crt` and passwordless `ca-certificates.p12` outputs for the guest's
egress gateway. The existing Agentplane egress Bundle emits both. ConfigMap disks
are boot snapshots: stop/start the VM after a bundle update, including CA rotation.

Use a disposable `agentplane-vm-prototype-*` namespace and fresh disks. Never attach
an existing environment's state. Retire `format_blank_disks` in the config disk
after the first successful runner RPC. Normal VM deletion retains the state disks;
experiment cleanup must explicitly remove its retained PVCs and separately created
cluster-scoped admission/secret-store resources.

## Evidence and remaining checks

[Recorded live acceptance](../../../../agentplane/debug/kubevirt/runtime-20261003.md)
identifies the tested OCI digest, boot/resource/recovery results, and limitations.
Those results use an earlier driver and image. The direct driver and updated trust-manager
PKCS#12 boot path have not yet been rerun live or timed.

The HTTP model fixture does not validate real gateway credential substitution or
CONNECT/TLS. Verify those before enabling deployed VM environments. Image assembly,
launcher readiness, and guest runner RPC readiness are distinct checks.
