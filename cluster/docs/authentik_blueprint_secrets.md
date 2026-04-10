# Authentik Blueprint Secret Injection

How secret values (OAuth client secrets, bearer tokens, API keys) get into
Authentik, and the trade-offs of each approach.

## Why Blueprints Are Special

Authentik's REST API and Terraform provider **do not allow setting a token to a
known key value**. The `key` field on `authentik_token` is server-generated and
read-only in both the API serializer and the Terraform resource. The only
exception is `AUTHENTIK_BOOTSTRAP_TOKEN` (limited to one admin token).

**Blueprints are the only supported mechanism for setting a token to a pre-known
value.** The blueprint serializer context specifically unlocks the `key` field as
writable — a privilege not available through any other interface.

## External Approaches (Non-Blueprint)

### Authentik Terraform Provider (`goauthentik/authentik`)

Declared in `cluster/terraform.tf` but intentionally unused. The codebase avoids
it due to state lifecycle coupling — when Authentik's DB is wiped, Terraform state
becomes stale, causing resource ID conflicts and cascading failures. See
<lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md>.

The provider can create service accounts and tokens, but the token key is
**server-generated**. You can only read it back (`retrieve_key = true`) and
propagate it to K8s Secrets. The flow is Authentik-outward, not inward.

```hcl
resource "authentik_token" "svc" {
  identifier   = "my-svc"
  user         = authentik_user.svc.id
  expiring     = false
  retrieve_key = true  # read-only, server-generated
}

# Propagate outward — cannot push a known value in
resource "kubernetes_secret" "svc_token" {
  metadata { name = "svc-token" }
  data = { token = authentik_token.svc.key }
}
```

### Authentik REST API

Same limitation — `POST /api/v3/core/tokens/` does not accept a `key` field.
`GET /api/v3/core/tokens/{identifier}/view_key/` retrieves an existing key.

### `AUTHENTIK_BOOTSTRAP_TOKEN`

Sets a single admin token to a known value on startup. Already used for the
bootstrap token. Limited to exactly one token.

### Crossplane Provider

Community `crossplane-contrib/provider-authentik` wraps the Terraform provider.
Same `key` limitation — server-generated, read-only.

### Summary

| Approach | Set token to known value? | State coupling? | Used in repo? |
|---|---|---|---|
| Blueprint `!Env` | **Yes** (unique to blueprints) | None | Yes |
| TF provider | No (server-generated) | Yes | No (intentionally) |
| REST API | No (server-generated) | N/A | No |
| Bootstrap token | Yes (one only, already used) | None | Yes |
| Crossplane | No (wraps TF provider) | Yes | No |

## Secret-Capable Blueprint Tags

Authentik blueprints support 14 custom YAML tags. Three can inject secret values:

| Tag | Source | Example |
|-----|--------|---------|
| `!Env` | Pod environment variable | `key: !Env GROCY_BEARER_TOKEN` |
| `!File` | File on disk (volume mount) | `key: !File /secrets/grocy/token` |
| `!Context` | Blueprint instance context (DB) | `key: !Context grocy_token` |

All three support defaults: `!Env [VAR, fallback]`, `!File [/path, fallback]`,
`!Context [key, fallback]`.

## How Secrets Reach the Blueprint

### `!Env` (current pattern)

K8s Secret -> `envFrom` in HelmRelease -> env var on Authentik pods -> `!Env VAR`.

**Secret sources:**
- SOPS-encrypted Secret in git (Flux decrypts)
- External Secrets Operator pulling from Vault
- Terraform-generated K8s Secret (alloy-otlp pattern)

**Pros:** Established pattern in this repo. Secret stays in K8s Secret, never in
ConfigMap.
**Cons:** Changing the env var value does not trigger blueprint reapplication (see
below).

### `!File` (volume mount)

K8s Secret -> volume mount in HelmRelease -> file at known path -> `!File /path`.

**Secret sources:** Same as `!Env` (SOPS, ESO, Terraform). The Secret is mounted
as a file instead of injected as an env var.

**Pros:** Better for multi-line secrets (certificates, keys). Avoids env var
namespace pollution.
**Cons:** Requires adding `extraVolumes`/`extraVolumeMounts` in Helm values
instead of `envFrom`. Same reapplication limitation as `!Env`. Not used in this
repo currently.

### `!Context` (database-stored)

API call or Terraform provider -> `BlueprintInstance.context` JSON field in DB ->
`!Context key`.

**Secret sources:** Authentik Terraform provider, Authentik REST API
(`PATCH /api/v3/managed/blueprints/{pk}/`).

**Pros:** Changing context triggers reconciliation (detects changes automatically).
**Cons:** Secrets stored in plaintext in the Authentik PostgreSQL database. Requires
Terraform or an external controller to set the context values.

## Reapplication Behavior

Authentik hashes the **raw blueprint file bytes** (SHA-512) and stores the hash in
`BlueprintInstance.last_applied_hash`. The hourly discovery cycle skips blueprints
whose file hash matches the stored hash.

Since `!Env` and `!File` are literal text in the YAML file, changing the underlying
secret value does **not** change the file hash. The blueprint will not be
automatically reapplied.

### Triggers for reapplication

| Trigger | Bypasses hash check? | Notes |
|---------|---------------------|-------|
| File content change (new commit) | Yes (hash differs) | Change a comment to force |
| inotify file modification event | Yes | File watcher calls `apply_blueprint` directly |
| Pod startup (worker boot) | No | Runs discovery, which checks hash |
| Manual API call | Yes | `POST /api/v3/managed/blueprints/{pk}/apply/` |
| `!Context` change | Yes | Context is in DB, change triggers reconcile |
| Hourly discovery cron | No | Compares hash, skips if unchanged |

### Practical rotation pattern

For `!Env`/`!File` secrets that rarely rotate:

1. Update the SOPS secret in git (new token value)
2. Bump a comment in the blueprint YAML (e.g., `# secret-version: 2`)
3. Commit and push both changes together
4. Flux updates the K8s Secret -> Reloader restarts Authentik pods
5. Discovery sees new file hash -> reapplies blueprint with new env var value

## Token Key Behavior in Blueprints

`state: present` with a `key` field **does update** existing token keys in
blueprints. The blueprint serializer context unlocks the `key` field as writable
with `partial=True`. The delete+recreate pattern (`state: absent` then
`state: present`) used in `alloy-otlp-sso.yaml` is a safety measure but is not
strictly required.

## Comparison Table

| | `!Env` | `!File` | `!Context` |
|---|---|---|---|
| Secret at rest | K8s Secret | K8s Secret | Authentik DB (plaintext) |
| Helm config | `envFrom` | `extraVolumes` + mounts | None (API-managed) |
| Auto-reapply on change | No | No | Yes |
| Rotation requires file change | Yes | Yes | No |
| Used in this repo | Yes (all SSO blueprints) | No | No |
| Terraform required | No | No | Yes (or API controller) |

## Recommendation

Use `!Env` with SOPS-encrypted Secrets for new blueprints. It is the only
mechanism that both (a) allows setting a token to a known value and (b) keeps the
secret in K8s Secrets rather than ConfigMaps or the database. The rotation
limitation is acceptable for tokens that change rarely — bump a blueprint comment
to force reapplication.
