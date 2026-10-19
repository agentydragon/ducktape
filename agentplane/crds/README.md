# Agentplane CRDs

`Sandbox`, `SandboxTemplate`, and `EgressCredential` schemas come from their Go API definitions and
kubebuilder markers. Bazel runs the pinned `controller-gen` with a declared Go SDK and GoPath.
`generate.py` keeps the Sandbox `PersistentVolumeClaimSpec` opaque while retaining its generated
`volumeMode` property, which the workspace CEL rule must inspect. It writes the three CRDs here.

After changing those types or markers, run `bb run //agentplane/crds:generate_bin` and commit the
generated manifests. `//agentplane/crds:test_generate` checks the committed outputs.

`PolicySet` and `PolicyBinding` are authored in this directory. The generator updates
`PolicyBinding.spec.subject` from its Go `ServiceAccountRef` type.
