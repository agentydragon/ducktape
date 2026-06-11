# Authentik API Token Vault Overwrite (State Loss → Silent Corruption)

**Date**: 2026-02-13
**Status**: Resolved (Vault rollback + CAS protection)

## Timeline

1. **04:46 UTC** — Bootstrap runs `authentik-token` Terraform module. Runner generates
   password v1 (`mxo0BchR...`), writes to Vault at `kv/sso/client-secrets`. Runner pod
   crashes before persisting tfstate to its K8s secret backend.
2. **~05:00 UTC** — Authentik Bootstrap Job reads token from Vault (v1), writes to
   PostgreSQL with `state: created` (write-once — never updates existing value).
3. **11:04 UTC** — tofu-controller retries `authentik-token` with fresh state (no prior
   state in K8s secret). `random_password` generates new v2 (`8AIvuSeO...`).
   `vault_kv_secret_v2` unconditionally creates version 2, overwriting v1.
4. **11:04+ UTC** — ESO syncs new token (v2) to `authentik-api-token` K8s secret.
   All tofu-controller Terraform resources using Authentik API get 403 (Authentik DB
   has v1, K8s secret has v2).

## Root Cause

`ignore_changes = [data_json]` on `vault_kv_secret_v2` only prevents UPDATE when
Terraform state exists. When state is lost (runner crash, K8s secret deleted), Terraform
sees no prior resource → performs CREATE → unconditionally overwrites Vault.

For write-once secrets (DB passwords, bootstrap tokens), the application side persists
the first value and never reads updates. Vault overwrite creates an irreconcilable split.

## Resolution

### Immediate: Vault Rollback

```bash
ROOT_TOKEN=$(kubectl get secret -n vault instance-unseal-keys \
  -o jsonpath='{.data.vault-root}' | base64 -d)
kubectl exec -n vault instance-0 -c vault -- sh -c \
  "VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/vault/tls/ca.crt \
   VAULT_TOKEN=$ROOT_TOKEN vault kv rollback -version=1 kv/sso/client-secrets"

kubectl annotate externalsecret authentik-api-token -n flux-system \
  force-sync=$(date +%s) --overwrite
```

### Permanent: CAS Protection

Added `cas = 0` (Check-And-Set) to all write-once `vault_kv_secret_v2` resources.
This makes the Vault write fail if the secret already exists at any version,
turning silent corruption into a loud Terraform error.

**Recovery from CAS failure** (state lost but Vault has correct value):

```bash
# Import existing Vault secret into Terraform state
terraform import vault_kv_secret_v2.authentik_api_token kv/data/sso/client-secrets
```

## Affected Resources

Write-once secrets with `cas = 0` protection:

| Module                | Vault Path               | Risk if Overwritten                |
| --------------------- | ------------------------ | ---------------------------------- |
| `authentik-token`     | `kv/sso/client-secrets`  | Authentik API access breaks        |
| `authentik-passwords` | `kv/authentik/passwords` | Authentik DB + admin access breaks |
| `powerdns-api-key`    | `kv/powerdns/api-key`    | DNS management breaks              |
| `harbor-admin`        | `kv/harbor/admin`        | Harbor admin access breaks         |
| `gitea-admin`         | `kv/gitea/admin`         | Gitea admin access breaks          |
| `atuin-secrets`       | `kv/atuin/secrets`       | Atuin DB access breaks             |
| `matrix-secrets`      | `kv/matrix/secrets`      | Matrix signing + DB access breaks  |

## Key Lessons

1. **`ignore_changes` does not protect against state loss** — it only prevents
   Terraform from detecting drift during plan. On fresh create (no state), it has
   no effect.
2. **`cas = 0` is the correct guard for write-once secrets** — Vault rejects the
   write if any version exists, regardless of Terraform state.
3. **Write-once vs rotatable** — secrets persisted at init time (DB passwords,
   bootstrap tokens) need CAS protection. Secrets consumed as env vars (OIDC
   client secrets, API keys) can be safely overwritten because both sides update.
4. **Vault KV versioning enables rollback** — `vault kv rollback -version=N`
   creates a new version identical to the specified old version, without needing
   to know the actual secret value.
