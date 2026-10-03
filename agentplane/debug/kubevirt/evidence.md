# Platform experiment: 2026-10-03 UTC

Environment: disposable `agentplane-vm-prototype-20261003`; KubeVirt v1.8.2,
Kyverno v1.19.1, KVM worker `ovh-ns103711`. Existing Agentplane namespaces and
existing VMs were untouched. Guest root image is pinned in the [reproduction guide](../../../cluster/cdk8s/agentplane/kubevirt_experiment/README.md).

| Probe                            | Observed result                                                                                                                                                                                                         |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Real KubeVirt launcher admission | Both VMIs reached Running; launcher containers 4/4 ready.                                                                                                                                                               |
| ServiceAccount defaulting order  | Final Pod account is VM-owned `vm-a`, `automountServiceAccountToken=false`; reviewed token uses `vm-a`, not `default`.                                                                                                  |
| Token mount isolation            | Only `egress-sidecar` mounts `agentplane-egress-token`; compute and disk containers do not. Guest sees its root and cloud-init disks and no Kubernetes serviceaccount mount.                                            |
| Guest-to-relay route             | `http://10.0.2.1:3128` reaches relay bound to Pod `0.0.0.0`; synthetic request returned HTTP 200.                                                                                                                       |
| Actual TokenReview               | Account `system:serviceaccount:agentplane-vm-prototype-20261003:vm-a`, bound Pod UID `565958ec-da15-454f-9072-efe255b8faa8`.                                                                                            |
| Unauthenticated direct gateway   | HTTP 407. TCP connectivity to the allowed gateway itself is expected.                                                                                                                                                   |
| Direct internet                  | Guest HTTPS to `1.1.1.1` timed out under the launcher fence.                                                                                                                                                            |
| Cross-VM access                  | Guest A requests to guest B's Pod IP on ports 3128, 3129 and 7000 timed out. Relay/readiness on B were listening. Guest B also timed out reaching A's known-listening port 7000, while the selected gateway reached it. |
| Forged launcher                  | Server dry-run copied the real launcher's valid owner chain under a different caller; Kyverno `authenticate-launcher` denied it.                                                                                        |
| Token rotation                   | Token `iat` advanced from `1790997779` to `1790998269` (600-second lifetime). Guest requests still authenticated with the same Pod UID; no relay restart.                                                               |
| Registry access                  | ESO produced the prototype pull secret through a separate namespace-restricted store and named-secret reader. No credential value was read by the experiment.                                                           |

[Remote tests and library checks](https://app.buildbuddy.io/invocation/89a45a52-c74d-45e9-a921-f267b2089a9d)
passed: reinvocation equality; forged caller, stale VMI/VM UID, wrong account owner;
compute/init/ephemeral token-mount rejection. This run checked the policy, VM fixture,
and setup libraries with the repository's lint/type aspects.

The identity endpoint is a TokenReview fixture, not the production egress gateway.
These results do not establish credential substitution, CONNECT/TLS, runner gRPC,
persistent state recovery, guest resource isolation, or an actual admission outage.

After stop/start, the recreated relay's request was reviewed with the new Pod UID
`01ef282b-ed21-4f28-b632-119c9d3a5e8a`. The disposable namespace, policy, ESO store,
reviewer role/binding, and named registry reader role/binding were removed after
these probes; namespace deletion completed. No persistent user disks were involved.

The move into `cluster/cdk8s/agentplane/kubevirt_experiment/` produced byte-identical
manifests. [Moved-package tests and library checks](https://app.buildbuddy.io/invocation/1c522e22-48b0-4ea9-85b3-45bad4883964)
also passed.
