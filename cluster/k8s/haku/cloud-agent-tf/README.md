# Haku cloud agent — Terraform control plane (deployed)

Deploys Haku's Anthropic-hosted (cloud) Managed Agent declaratively. The
tofu-controller runs the root at <../../../../tf/gitops/haku-cloud-agent> (the
`claude-managed-agents` provider creates the environment, agent, vault,
static_bearer credential, and deployment). Haku reaches the cluster through the
`kubectl-machine-mcp` passthrough MCP — see
<../../agents/kubectl-machine-mcp/README.md>. Architecture and the forward plan:
<../../../../haku/runtime/managed_agent/anthropic_hosted/README.md>.

## Manifests

- `terraform.yaml` — the `Terraform` CR. Injects `ANTHROPIC_API_KEY`
  (spend-capped workspace key) into the runner; publishes the provisioned
  resource IDs to the `haku-cloud-agent-ids` Secret (`writeOutputsToSecret`).
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
