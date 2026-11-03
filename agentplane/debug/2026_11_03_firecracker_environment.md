# Firecracker environment implementation validation

This implementation follows the [primitive spike](2026_11_02_firecracker_spike.md). The backend
contract is [Firecracker Sandboxes](../docs/firecracker_sandboxes.md). Source work started from
`origin/devel` at `e6cac7bf9cbc9ee428ef3487a4af4ddab17e2a0f`.

## Completed source checks

- [Controller, command, CRD admission, Gateway and environment-view tests](https://app.buildbuddy.io/invocation/8fb70a61-52a9-44c7-90c4-130302b044b9):
  all six targets passed. Admission tests exercise every template-arm combination and storage
  layout constraints. Controller tests retain the Pod and credentials, skip Quiesce during RAM
  suspension, hold missing launchers, fence stale supervisor UIDs and ignore pre-resume idle facts.
- [Firecracker chart and direct library lint aspects](https://app.buildbuddy.io/invocation/324f7b8d-19bc-443d-b892-bd01a30f1bee):
  passed. The template test checks relay-only projected tokens, explicit identities, native sidecar
  startup, privileged launcher, Runtime config and the read-only status network policy.
- [Existing Pod/KubeVirt template and Pod-policy regressions](https://app.buildbuddy.io/invocation/873096e1-eaa8-49de-9839-13087fed8c24):
  all four tests passed.
- [Launcher checkpoint, lifecycle-ordering and firewall tests](https://app.buildbuddy.io/invocation/59bdfa95-9e73-4e09-b6ad-e2afa40f4059):
  all three targets passed, including Ruff and mypy aspects. Subsequent block-device initialization
  changes require a fresh run.
- [Launcher OCI image](https://app.buildbuddy.io/invocation/ba0205a6-0cba-45f5-9cab-c00343bce152):
  built successfully with pinned Firecracker 1.17.0 and its disk/network tooling.
- [mTLS Runtime probe](https://app.buildbuddy.io/invocation/81dbe03b-ad9a-4a10-8e42-2039805c9400):
  built successfully. It calls WatchState and GetStatus using Gateway credentials, an explicit
  connection epoch and an optional expected Runtime process boot ID.

The new guest thaw-agent test and runner EROFS extraction test pass. Both NixOS configurations
evaluate. The new guest kernel and OCI image are still building; these source checks alone do not
establish that the image boots or that a real Runtime resumes.

## Deployment boundary

The backend is disabled in staging. The implementation retains its launcher Pod across suspend;
Kubernetes scheduling reservations remain allocated. Missing/failed launchers are held for recovery,
not replaced or automatically cold booted. There is no cross-node restore or jailer in this change.

An isolated `firecracker-environment-proof` namespace on the existing `wyrm2` KVM node is reserved
for live boot/resume acceptance. Its launcher has no ServiceAccount token and no host network/PID
namespace. The proof uses synthetic CA/Runtime/Gateway credentials, never production credentials.
Results will distinguish this manual Runtime protocol probe from an actual deployed Gateway,
Policy Controller, egress TokenReview and Session/harness end-to-end test.
