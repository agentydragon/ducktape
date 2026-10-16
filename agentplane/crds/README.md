# Agentplane CRDs

`Sandbox` and `SandboxTemplate` schemas come from the shared Go API definitions and their
kubebuilder markers. Bazel runs the pinned `controller-gen` with a declared Go SDK and GoPath.
`generate.py` keeps `PersistentVolumeClaimSpec` opaque while retaining its generated `volumeMode`
property, which the workspace CEL rule must inspect. It then writes the two CRDs here and their
kubeconform schemas under `cluster/schemas/`.

After changing those types or markers, run `bb run //agentplane/crds:generate_bin` and commit the
generated manifests and schemas. `//agentplane/crds:test_generate` checks the committed outputs.

The EgressCredential, PolicySet, and PolicyBinding CRDs are authored in this directory. The same
generator updates PolicyBinding's subject schema from `ServiceAccountRef` and writes their
kubeconform schemas.
