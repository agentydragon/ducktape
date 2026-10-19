# Agentplane CRDs

`Sandbox`, `SandboxTemplate`, `EgressCredential`, `PolicySet` and `PolicyBinding` schemas come from
their Go API types and kubebuilder markers. Bazel runs the pinned `controller-gen` with a declared
Go SDK and GoPath. `generate.py` keeps `PersistentVolumeClaimSpec` opaque while retaining its
generated `volumeMode` property, which the workspace CEL rule must inspect. PolicySet rule and
ActionPolicy unions are enforced with CEL; its JSON Schema payload stays opaque.

After changing those types or markers, run `bb run //agentplane/crds:generate_bin` and commit the
generated manifests. `//agentplane/crds:test_generate` checks committed outputs and publishes the
expected manifests as test artifacts when they drift. PolicyBinding's Go schema owns subject
validation and the condition list's field ownership; source and target union constraints use
generated CEL rules. `//agentplane/crds:test_sandbox_admission` and
`//agentplane/crds:test_policyset_admission` exercise the schemas through the real API server.
