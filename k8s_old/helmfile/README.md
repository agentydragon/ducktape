# Helmfile Orchestration

The charts under `k8s/helm/` are still the single source of truth, but you can now deploy the whole stack at once via Helmfile.

## Prerequisites

- `helm` and `helmfile` installed locally.
- Access to the cluster (`kubectl config current-context` should point at k3s).
- Container registry pushes are already in place for images referenced in the chart values.

## Usage

```bash
cd k8s/helmfile
helmfile repos       # one-time repo sync
helmfile apply       # install/upgrade all releases
```

Releases are applied in dependency order:

1. `sealed-secrets` – Bitnami sealed-secrets controller
2. `traefik` – ingress controller DaemonSet and TLS defaults
3. `cert-manager` – homelab CA issuers
4. `authentik` – SSO stack with PostgreSQL/Redis deps
5. `gitea` – Git server + Authentik automation
6. `ember` – agent runtime + PAT rotator job
7. `rspcache` – proxy/admin deployments and PostgreSQL

Each chart’s default `values.yaml` already reflects the current k3s cluster configuration. To override anything cluster-specific, drop a file under `values/` and add it to the relevant release in `helmfile.yaml`.

## Inspecting Changes

Dry-run everything without touching the cluster:

```bash
helmfile diff
# or helmfile template to render manifests without applying them
```

You can still operate on individual releases if needed:

```bash
helmfile --selector name=traefik apply
helmfile --selector namespace=gitea diff
```

That keeps per-service lifecycles manageable while retaining a single entrypoint for full-cluster reconciles.
