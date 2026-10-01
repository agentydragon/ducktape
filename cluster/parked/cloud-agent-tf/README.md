# Haku cloud agent (parked)

The Flux Kustomization and Terraform resource remain suspended. The Anthropic-hosted
Managed Agent's environment, agent, deployment, vault, and four MCP credentials were
deleted at Anthropic on 2026-09-30. This tree preserves only the cluster-side wiring
and historical Terraform root; it does not represent live Anthropic state.

The HCL root lives with the parked component at
<../../../haku/runtime/x/managed_agent/anthropic_hosted/terraform>. Its Bazel target
is `manual`, and the provider is omitted from the global rules_tf mirror because its
published GitHub release assets currently do not resolve. See the component's
<../../../haku/runtime/x/managed_agent/anthropic_hosted/README.md> before proposing
reactivation.

## Parked cluster resources

- `terraform.yaml` — suspended tofu-controller resource. Its source path points to
  the historical HCL root. The `haku-cloud-agent-ids` Secret may still exist in the
  cluster, but its vault ID refers to the deleted vault; do not use those values.
- `anthropic-api-key-eso.yaml` — the ExternalSecret wiring for the dedicated,
  spend-capped Anthropic workspace key. The source credential remains shared with
  LiteLLM; this parked consumer does not own or revoke it.
- `external-creds-reader.yaml` — read access required by the parked ExternalSecret.
- The old `haku-cloud-kube-token` Secret seed was removed. The Authentik `haku-k8s`
  rotation still writes its source JWT for CLI use, but no longer publishes a copy for
  this parked agent. A future Terraform apply must deliberately restore that input.

## Reactivation requirements

Do not resume this Kustomization by itself. A reviewed reactivation must first choose
whether to keep or replace the Terraform provider, repair the root for fresh state,
recreate the Anthropic objects and vault credentials, and restore the cluster token
input if the cloud agent still uses `kubectl-machine-mcp`. Only then should the
Terraform resource be resumed and its newly written IDs consumed.
