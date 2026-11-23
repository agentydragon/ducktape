"""Shared API names for policy resources (`agentplane/crds/manifests/`).

The Go runtime reads PolicySets. Expiry is not a reader's concern: the Policy Controller deletes a
binding once its `expiresAt` passes, so a binding that exists grants.
"""

POLICY_SETS_PLURAL = "policysets"
POLICY_BINDINGS_PLURAL = "policybindings"
# Flux stamps this label on objects it owns and will reapply after runtime deletion.
FLUX_KUSTOMIZATION_LABEL = "kustomize.toolkit.fluxcd.io/name"
