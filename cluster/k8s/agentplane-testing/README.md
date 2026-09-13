# Agentplane testing deployment artifact

The environment-level Flux Kustomization consumes the `agentplane-testing`
ExternalArtifact at the environment root. Its artifact contains the complete
Kustomize input closure for testing; the Flux declaration itself remains owned
by the root Kustomization and is excluded from the artifact.

The nested Kustomization files remain local bases for the environment root.
The consumer explicitly checks the trust-manager-generated CA ConfigMap and
Bundle because the ConfigMap is created asynchronously outside the artifact.
