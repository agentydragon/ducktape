# Haku cloud agent — Terraform control plane (parked)

This Terraform path is suspended and parked. It used to deploy Haku's
Anthropic-hosted (cloud) Managed Agent declaratively, but the cloud objects were
deleted at Anthropic and the `claude-managed-agents` provider did not report the
deletions during Read. The matching Bazel `tf_module` is tagged `manual`, and
`modus-agendi/anthropic-claude-managed-agents` is omitted from the global
rules_tf provider mirror, because v1.1.0's GitHub repo/release assets currently
404 even though the OpenTofu registry still advertises cached metadata. Keeping
it in the global mirror breaks unrelated Terraform/image jobs.

The HCL root remains at <../../../../tf/gitops/haku-cloud-agent> as historical
state and as input to the Haku agent SSOT drift guard. Explicit
validate/apply-style Bazel targets need the provider restored or replaced.
Re-enable normal CI coverage only after choosing the path in
<../../../../haku/runtime/managed_agent/anthropic_hosted/README.md>: replace or
restore the provider and recreate the cloud agent, or retire this root in favor
of imperative provisioning. Haku reaches the cluster through the
`kubectl-machine-mcp` passthrough MCP — see
<../../agents/kubectl-machine-mcp/README.md>.

## Manifests

- `terraform.yaml` — the suspended `Terraform` CR. Injects `ANTHROPIC_API_KEY`
  (spend-capped workspace key) into the runner when resumed; publishes the
  provisioned resource IDs to the `haku-cloud-agent-ids` Secret
  (`writeOutputsToSecret`).
- `anthropic-api-key.sops.yaml` — the workspace API key.
- `haku-kube-token.sops.yaml` — the rotated `haku-k8s` Authentik JWT
  (`haku-cloud-kube-token` Secret), read in-cluster by the tofu root and pushed
  into the Anthropic vault as the static_bearer credential. Rotated by
  `authentik-jwt-rotation`; the chain is fully automatic (see the main.tf header).

## Provisioned IDs

The tofu root's outputs (`agent_id`, `deployment_id`, `environment_id`,
`vault_id`) are written to the **`haku-cloud-agent-ids`** Secret in `flux-system`
— the canonical, machine-readable source. Don't hardcode these literals; read
them from the Secret so they can't drift.

```bash
kubectl -n flux-system get secret haku-cloud-agent-ids \
  -o go-template='{{range $k,$v := .data}}{{$k}}={{$v | base64decode}}{{"\n"}}{{end}}'
```

## Run / smoke-test

The deployment is on-demand (v0: its initial event lists `haku-sandbox` pods —
a connectivity test). Trigger a run with the `deployment_id` read from the Secret,
then watch the session in the Console (`platform.claude.com/workspaces/default/sessions`):

```bash
DEPL_ID=$(kubectl -n flux-system get secret haku-cloud-agent-ids \
  -o jsonpath='{.data.deployment_id}' | base64 -d)
ant beta:deployments run --deployment-id "$DEPL_ID"
```
