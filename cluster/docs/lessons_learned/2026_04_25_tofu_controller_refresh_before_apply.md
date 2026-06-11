# JWT rotation pipeline failures (2026-04-25)

## Incident

The `claude-jwt-rotation` CronJob failed to mint and commit a working JWT for
Claude Code web sessions. Multiple layered issues were discovered and fixed
during a single debugging session.

## Root causes (5 issues, in order of discovery)

1. **Wrong token endpoint URL**: `rotate.sh` used a per-application token path
   (`/application/o/kubectl-sandbox-client-credentials/token/`) which returns
   HTTP 405. Authentik uses a shared token endpoint (`/application/o/token/`).

2. **Missing policy binding for auto-created user**: Authentik auto-creates an
   internal service account (`ak-<slug>-client_credentials`) for
   `client_credentials` grants. The TF-managed policy binding only authorized
   the TF-created service account (different user). The auto-created user
   was rejected with `invalid_grant` even at the correct endpoint.

3. **Missing `secrets/` directory on first rotation**: The script writes to
   `secrets/claude-web-k8s-jwt.yaml` but the sparse checkout doesn't create
   the parent directory when bootstrapping. Fixed with `mkdir -p`.

4. **SOPS `time.Time` parse failure**: Unquoted ISO 8601 timestamps in YAML
   are parsed by SOPS as Go `time.Time` values, which SOPS can't encrypt.
   Fixed by quoting the `expires_unencrypted` value.

5. **Missing `profile` and `email` scopes**: The `client_credentials` grant
   only requested `openid groups`. kube-apiserver's `AuthenticationConfiguration`
   maps `preferred_username` (from the `profile` scope) to the username claim.
   Without it, the JWT was minted successfully but rejected by the apiserver
   as Unauthorized.

### Preventive issue (not directly causal)

**`refreshBeforeApply: false`** on all 15 tofu-controller Terraform CRs.
During investigation we initially suspected Authentik data loss (applications
missing from the DB while providers existed). This turned out to be a
pagination bug in the API query, but the vulnerability is real: without
refresh, the controller trusts state unconditionally and would silently miss
any future state/reality divergence. Enabled `refreshBeforeApply: true` on
all CRs as a preventive measure.

## Detection

- CronJob failed with `curl: (22) ... 405` / `400`.
- Loki logs (via `promtail`) preserved pod output even after the
  `backoffLimit: 0` Job deleted the pod — critical for debugging.
- Manual `curl` against Authentik token endpoint and API to trace each
  failure layer.

## Fixes applied

| Commit    | Fix                                                                         |
| --------- | --------------------------------------------------------------------------- |
| `47bc0db` | `refreshBeforeApply: true` on all 15 TF CRs; `TOKEN_URL` fix; roadmap TODOs |
| `3374ba4` | Policy binding for Authentik auto-created `client_credentials` user         |
| `1e17f8a` | `mkdir -p` before writing SOPS file                                         |
| `777a84f` | Quote ISO timestamp to avoid SOPS `time.Time` parse                         |
| `dc60597` | Request `profile email` scopes for `preferred_username` claim               |
| `07c8cec` | Strip quotes in freshness check `sed` → `date` pipeline                     |

## Lessons

- **Authentik `client_credentials` creates its own user.** The flow
  authenticates as `ak-<slug>-client_credentials`, not as any user you
  manually create. Policy bindings must include this auto-created user.

- **Authentik token endpoints are shared**, not per-application. The OIDC
  discovery document (`.well-known/openid-configuration`) returns the correct
  `token_endpoint` — always use discovery rather than constructing URLs.

- **Request all scopes your consumer needs.** `client_credentials` grants only
  include claims for requested scopes. If kube-apiserver maps
  `preferred_username`, you must request the `profile` scope that provides it.

- **Always enable `refreshBeforeApply: true`** for tofu-controller Terraform
  resources that manage external state. The cost is one extra API call per
  reconcile; the benefit is automatic state/reality divergence detection.

- **SOPS and YAML datetime literals don't mix.** Always quote ISO 8601
  timestamps in SOPS-encrypted YAML files. Unquoted values are parsed as
  Go `time.Time`, which SOPS can't walk. The freshness-check reader must
  also strip the quotes.

- **Test the full pipeline, not just individual steps.** Each fix exposed the
  next failure. A single end-to-end test (mint → encrypt → commit → build
  kubeconfig → `kubectl auth whoami`) would have caught all five issues at
  once.
