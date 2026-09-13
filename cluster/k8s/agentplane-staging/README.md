# Agentplane staging deployment artifact

The environment-level Flux Kustomization consumes the `agentplane-staging`
ExternalArtifact at the environment root. Its artifact contains the complete
Kustomize input closure for staging; the Flux declaration itself remains owned
by the root Kustomization and is excluded from the artifact.

The nested Kustomization files remain local bases for the environment root.
SOPS-encrypted Actions data is decrypted by the environment-level Flux consumer.
The consumer explicitly checks the trust-manager-generated CA ConfigMap and
Bundle because the ConfigMap is created asynchronously outside the artifact.
