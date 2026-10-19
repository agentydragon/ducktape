# Agentplane CRDs

`Sandbox`, `SandboxTemplate`, `EgressCredential` and `PolicyBinding` schemas come from their shared
Go API types and kubebuilder markers. Bazel runs the pinned `controller-gen` with a declared Go SDK
and GoPath. `generate.py` keeps `PersistentVolumeClaimSpec` opaque while retaining its generated
`volumeMode` property, which the workspace CEL rule must inspect.

After changing those types or markers, run `bb run //agentplane/crds:generate_bin` and commit the
generated manifests. `//agentplane/crds:test_generate` checks committed outputs and publishes the
expected manifests as test artifacts when they drift. PolicyBinding's Go schema owns subject
validation and the condition list's field ownership; `agentplane.subjects.ServiceAccountRef` is
its Python wire representation. Source and target union constraints use generated CEL rules,
exercised through the real API server in `//agentplane/crds:test_sandbox_admission`.

PolicySet remains authored in this directory.
