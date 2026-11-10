# Agentplane CRDs

`Sandbox`, `SandboxTemplate`, `EgressCredential`, `PolicySet`, `PolicyBinding`, `Identity`,
`KubernetesRoleSet`, `ServiceAccountGrant`, `ManagedServiceAccount` and `WorkloadCredential` schemas come from their Go
API types and kubebuilder markers. Bazel runs the pinned `controller-gen` with a declared
Go SDK and GoPath. `generate.py` keeps `PersistentVolumeClaimSpec` opaque while retaining its
generated `volumeMode` property, which the workspace CEL rule must inspect. PolicySet rule and
ActionPolicy unions are enforced with CEL; its JSON Schema payload stays opaque.

After changing those types or markers, run `bb run //agentplane/crds:generate_bin` and commit the
generated manifests. `//agentplane/crds:test_generate` checks committed outputs and publishes the
expected manifests as test artifacts when they drift. PolicyBinding's Go schema owns the
condition list's field ownership; its subject union and the source and target union constraints
use generated CEL rules. `//agentplane/crds:test_{sandbox,policyset,identity,workloadcredential}_admission` exercise the schemas through the
real API server. CEL rules must fit the API server's cost budget: bound every list a rule iterates
or sits under with `MaxItems`.

`Sandbox.spec.resumePolicy.allowOwnServiceAccount` and `allowOwnIdentity` are optional and default to
false; the Runner Gateway's lifecycle manager enforces them
([SPEC](../runner_gateway/environments/lifecycle/SPEC.md)).
