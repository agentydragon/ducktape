# ESO Password Generator Desynchronization

**Date**: 2025-11-28
**Status**: Resolved (Vault SSOT migration in progress)

## Root Cause

ESO Password generators (`generators.external-secrets.io/v1alpha1 Password`) are
**stateless** — they generate a fresh random password on every `refreshInterval` sync,
independent of any source of truth. When applications persist credentials at init time
(PostgreSQL writes password to DB, Authentik Bootstrap Job writes token once), the
persisted value and the K8s Secret diverge after the first refresh.

## Affected Systems

### SSO/OIDC Credential Split

Two independent sources for the same client secret:

1. Terraform generates `random_password.result` → stores in Vault → creates Authentik provider
2. ExternalSecret uses ESO Password generator → generates **different** password → K8s Secret

Result: Authentik knows password A, application uses password B → "invalid client credentials".

**Fix**: Replace ESO Password generators with Vault data sources (Terraform generates once →
Vault stores → ESO reads stable value). Commit 05b5e5e.

### Init-Time Persistence Pattern

Applications that write secrets to database on first boot:

- **PowerDNS API key**: Written to PostgreSQL on init, immutable after
- **Authentik PostgreSQL password**: Set on DB creation via env var
- **Authentik Bootstrap token**: Job writes to DB once, Job is immutable

When ESO refreshes the K8s Secret with a new value, the database still has the old one.
Restarting the pod picks up the new secret but doesn't ALTER the DB password.

**Fix (Phase 0)**: Changed refresh intervals to 8760h (1 year) to stop regeneration.
Commit eaaf4b1.

**Fix (Phase 1)**: Stakater Reloader auto-restarts pods when secrets change. Handles
90% of cases (service-to-service auth, API keys consumed by pods). Does NOT fix
init-time persistence.

**Fix (Phase 2)**: Migrate Password generators to Vault KV sources. Terraform generates
once → stores in Vault → ESO reads stable value. Completed for: PowerDNS API key,
Authentik API token, Harbor admin password.

## Dependency Chain

```text
ESO Password Generator (stateless, regenerates every refresh)
  → Kubernetes Secret (mutable, changes on refresh)
    ├→ Application Pod (reads at start, no auto-restart)
    │    → Environment var becomes stale
    └→ Init Script / Job (writes to DB once, immutable)
         → Database value diverges from Secret
```

## Key Lessons

1. **ESO Password generators don't read — they generate** — every sync produces a new
   random value. They are NOT idempotent sources of truth.
2. **Never use Password generators for credentials managed by Terraform** — if Terraform
   creates an OIDC provider with password A, the ExternalSecret must read A from Vault,
   not generate independent password B.
3. **Never use Password generators for database init passwords** — the DB persists the
   first password; regenerating creates an irreconcilable split.
4. **Correct pattern**: Terraform generates → stores in Vault KV → ESO reads from Vault.
   Single source of truth, stable across syncs.
5. **Stakater Reloader is necessary but insufficient** — handles pod-level secret
   consumption but cannot fix init-time persistence (ALTER USER, recreate Job).

## Diagnosis

```bash
# Check if ExternalSecret uses Password generator (WRONG)
kubectl get externalsecret <app>-oidc-secret -n <namespace> -o yaml | grep -A5 "generatorRef"
# If you see "kind: Password" - this is the problem

# Compare passwords in Vault vs K8s secret
kubectl exec -n vault vault-0 -c vault -- \
  env VAULT_TOKEN=<token> vault kv get -field=client_secret kv/sso/<app>
kubectl get secret <app>-oauth-client-secret -n <namespace> \
  -o jsonpath='{.data.client_secret}' | base64 -d
# If they differ - desynchronized
```
