# Agentplane testing deployment artifact

The environment-level Flux Kustomization consumes the `agentplane-testing`
ExternalArtifact at the environment root. Its artifact contains the complete
Kustomize input closure for testing; the Flux declaration itself remains owned
by the root Kustomization and is excluded from the artifact.

`agentplane.k8s.yaml` is one generated chart for the whole environment (Namespace,
quota, RBAC, the model-catalog ConfigMap, and the workload surface -- db, llm-ingress,
egress, app, actions, Dex) -- see `cluster/cdk8s/agentplane/`. The consumer explicitly
checks the trust-manager-generated CA ConfigMap and Bundle because the ConfigMap is created
asynchronously outside the artifact.
