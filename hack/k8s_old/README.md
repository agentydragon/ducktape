# Legacy k3s Charts (Pending Migration)

Remnants of the old k3s cluster. Most services have been migrated to `cluster/k8s/` under
Flux GitOps. The charts below contain functionality not yet reproduced in the new cluster.

## Remaining Charts

| Chart          | Purpose                                          |
| -------------- | ------------------------------------------------ |
| `ember`        | Agent with Matrix/rspcache/Gitea PAT integration |
| `gitea`        | Ember bootstrap Job (PAT provisioning)           |
| `guacamole`    | Remote desktop gateway                           |
| `matrix-stack` | Ember-bot user provisioning for Matrix           |
| `rspcache`     | OpenAI response cache proxy                      |

## What Was Migrated

The following were fully replaced by `cluster/k8s/` equivalents and deleted:

- `authentik` → `k8s/authentik/` + SSO blueprints
- `cert-manager-bootstrap` → `k8s/cert-manager/` (Let's Encrypt)
- `common-lib` → Kustomize patterns
- `metallb` → not needed (Hetzner VPS hostNetwork)
- `observability` → `k8s/monitoring-stack/` + `k8s/loki/`
- `registry` → `k8s/applications/harbor/`
- `shared-secret` → ESO + Vault pattern
- `traefik` → `k8s/ingress-nginx/`
- `atuin` → `k8s/applications/atuin/`
