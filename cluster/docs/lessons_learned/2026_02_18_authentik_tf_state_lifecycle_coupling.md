# Authentik Terraform State Lifecycle Coupling

**Date**: 2026-02-18
**Status**: Resolved (manual state wipe + documentation)

## Root Cause

tofu-controller stores Terraform state as K8s secrets (`tfstate-default-*` in flux-system).
These secrets have **no lifecycle coupling** to the backend they manage. When Authentik's
database is wiped (HelmRelease + PVC deleted), all 11 SSO Terraform modules' state secrets
become stale — they reference Authentik resource PKs/UUIDs that no longer exist.

On reconciliation against the fresh Authentik instance:

1. Terraform reads stale state → tries to refresh resources by PK → gets 404
2. Provider marks resources for recreation (Authentik provider issue #104 fix)
3. Multiple modules apply simultaneously against the fresh DB
4. Partial applies create resources but crash before saving state (runner timeout, TLS
   cache desync, or resource conflicts from parallel creates)
5. Next retry reads partially-saved state → tries to create resources that already exist
6. **"already exists" errors cascade** — each module fails independently

The result is cross-contamination: one module's provider gets assigned to another module's
application, creating an inconsistent SSO configuration that requires manual cleanup of
every conflicting resource in the Authentik API.

## The Three-Layer Cascade

```text
LAYER 1: Authentik Database Wiped
  ├─ All built-in resources regenerated with new UUIDs (flows, groups, cert keypairs)
  ├─ All user-created resources destroyed (providers, applications, outposts)
  └─ PK counter resets — fresh DB assigns PKs sequentially (1, 2, 3...)

LAYER 2: Terraform State Secrets Are Stale
  ├─ 11 independent tfstate secrets reference PKs from OLD Authentik
  ├─ Each module stores state in its own K8s secret (no shared state)
  └─ No mechanism triggers state cleanup when Authentik is recreated

LAYER 3: Partial Apply Failures Cascade
  ├─ Module A creates provider (gets pk=1), crashes before saving state
  ├─ Module B creates provider (gets pk=2), saves state
  ├─ Module A retries with no state, creates provider again (gets pk=3)
  ├─ Module A creates application referencing pk=3
  ├─ Module B's stale state references pk=1 → cross-contamination
  └─ "Application with this provider already exists" errors propagate
```

## Affected Terraform State Secrets

All secrets in `flux-system` namespace matching `tfstate-default-authentik-blueprint-*`
plus related modules:

| Secret                                         | Terraform Module      | Authentik Resources                  |
| ---------------------------------------------- | --------------------- | ------------------------------------ |
| `tfstate-default-authentik-blueprint-users`    | `sso/users`           | User, custom flow, brand             |
| `tfstate-default-authentik-blueprint-gitea`    | `sso/gitea`           | OAuth2 provider, application         |
| `tfstate-default-authentik-blueprint-harbor`   | `sso/harbor`          | OAuth2 provider, application         |
| `tfstate-default-authentik-blueprint-hubble`   | `sso/hubble`          | Proxy provider, application, outpost |
| `tfstate-default-authentik-blueprint-loki`     | `sso/loki`            | Proxy provider, application, outpost |
| `tfstate-default-authentik-blueprint-matrix`   | `sso/matrix`          | OAuth2 provider, application         |
| `tfstate-default-authentik-blueprint-vault`    | `sso/vault`           | OAuth2 provider, application         |
| `tfstate-default-authentik-blueprint-openclaw` | `sso/openclaw`        | Proxy provider, application, outpost |
| `tfstate-default-grafana-sso`                  | `sso/grafana`         | OAuth2 provider, application         |
| `tfstate-default-vault-oidc-auth`              | `sso/vault-oidc-auth` | Vault OIDC auth backend              |

**Also affected (but different)**: `tfstate-default-authentik-token` and
`tfstate-default-authentik-passwords` generate secrets stored in Vault. These are protected
by `cas = 0` (Check-And-Set) — Vault rejects overwrites. Only delete these state secrets
during a full cluster rebuild where Vault is also wiped.

## Resolution

### When Tearing Down Authentik (HelmRelease + PVC Delete)

```bash
# 1. Suspend all Authentik-targeting Terraform resources
for name in authentik-blueprint-users authentik-blueprint-gitea \
  authentik-blueprint-harbor authentik-blueprint-hubble authentik-blueprint-loki \
  authentik-blueprint-matrix authentik-blueprint-vault authentik-blueprint-openclaw \
  grafana-sso vault-oidc-auth; do
  kubectl patch terraform "$name" -n flux-system \
    -p '{"spec":{"suspend":true}}' --type=merge 2>/dev/null
done

# 2. Kill runner pods
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner

# 3. Delete stale TF state secrets
kubectl delete secret -n flux-system \
  tfstate-default-authentik-blueprint-users \
  tfstate-default-authentik-blueprint-gitea \
  tfstate-default-authentik-blueprint-harbor \
  tfstate-default-authentik-blueprint-hubble \
  tfstate-default-authentik-blueprint-loki \
  tfstate-default-authentik-blueprint-matrix \
  tfstate-default-authentik-blueprint-vault \
  tfstate-default-authentik-blueprint-openclaw \
  tfstate-default-grafana-sso \
  tfstate-default-vault-oidc-auth \
  --ignore-not-found

# 4. If Vault persists (not wiped), also clean up its OIDC auth backend
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=$ROOT_TOKEN \
   vault auth disable oidc/" 2>/dev/null || true

# 5. Delete Authentik HelmRelease and PVC (Flux recreates from git)
kubectl delete helmrelease authentik -n authentik
kubectl delete pvc data-authentik-postgresql-0 -n authentik

# 6. Wait for Authentik to come back up
kubectl wait --for=condition=available deployment/authentik-server \
  -n authentik --timeout=1200s

# 7. Unsuspend Terraform resources
for name in authentik-blueprint-users authentik-blueprint-gitea \
  authentik-blueprint-harbor authentik-blueprint-hubble authentik-blueprint-loki \
  authentik-blueprint-matrix authentik-blueprint-vault authentik-blueprint-openclaw \
  grafana-sso vault-oidc-auth; do
  kubectl patch terraform "$name" -n flux-system \
    -p '{"spec":{"suspend":false}}' --type=merge 2>/dev/null
done
```

### When Rebuilding the Full Cluster (Bootstrap)

During a full `tofu destroy` → `bazel run //cluster:bootstrap` cycle, all K8s secrets
(including `tfstate-default-*`) are destroyed with the cluster. No manual cleanup needed —
the bootstrap creates everything from scratch.

## Why This Can't Be Automated (Yet)

| Mechanism                    | Why It Doesn't Help                                                         |
| ---------------------------- | --------------------------------------------------------------------------- |
| Flux `dependsOn`             | Controls apply order, not deletion cascade                                  |
| Flux `prune: true`           | Prunes Terraform CRs, not their state secrets                               |
| K8s OwnerReferences          | tofu-controller doesn't set them (issue #937, open since 2024)              |
| `destroyResourcesOnDeletion` | Requires the backend (Authentik) to be up during destroy                    |
| Authentik provider 404 fix   | Handles individual missing resources, not mass PK reassignment from DB wipe |

## Alternatives Considered

**Authentik native blueprints** (`state: present`): Idempotent YAML config mounted as
ConfigMaps into Authentik worker. No external state — blueprints re-apply every 60 minutes.
Eliminates the TF state coupling entirely. **Limitation**: can't manage Vault secrets or
cross-service orchestration. Could work for simple resources (users, flows, brands) while
keeping Terraform for OIDC providers that need Vault integration.

**Crossplane provider**: Continuous reconciliation from K8s CRDs. Would detect missing
resources and recreate them. **Limitation**: no Crossplane provider for Authentik exists.

**Authentik K8s operator**: Official feature request (issue #5675) confirmed but unassigned,
no timeline. Would provide CRD-based management with proper reconciliation.

## Key Lessons

1. **tofu-controller TF state is an implicit dependency on the managed backend** — when
   the backend is wiped, state must be wiped too. This coupling is not declared anywhere
   in the Flux dependency graph.
2. **11 independent Terraform modules = 11 independent failure points** — each module's
   state references the same Authentik instance. A single backend wipe invalidates all of
   them simultaneously, and parallel reconciliation causes cross-contamination.
3. **Partial applies are the worst failure mode** — resources created in Authentik but not
   recorded in TF state require manual API cleanup. "Already exists" errors don't
   self-resolve because the resource is real but stateless.
4. **The Authentik flow API uses slugs for DELETE, not UUIDs** — discovered during manual
   cleanup. `DELETE /api/v3/flows/instances/{slug}/` works; UUID-based DELETE returns 404.
5. **Suspend before wiping state** — if Terraform resources are active during state
   deletion, runners may partially apply and recreate stale state before cleanup completes.
