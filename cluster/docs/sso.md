# SSO Integration (Authentik)

## Two provider management patterns

### TF-managed providers (preferred for new providers)

`tf/gitops/sso-providers/` creates `authentik_provider_oauth2` resources directly.
TF owns the client_secret lifecycle — no Vault, no ESO, no `!Env` drift.

**Secret flow**: TF creates provider → reads `client_secret` → writes `kubernetes_secret`
in `authentik` namespace → Reflector mirrors to consumer namespace(s).

**Source of truth for which apps currently have SSO**: the `provider_*.tf` files
under `tf/gitops/sso-providers/` — one file per app. Don't hand-maintain an
enumeration here; it goes stale as apps are added/removed. `ls tf/gitops/sso-providers/`
(or grep for `authentik_provider_oauth2`) for the current set.

Every provider file follows the same shape — an `authentik_provider_oauth2` +
`authentik_application`, sharing the same `authorization_flow`/`invalidation_flow`,
`issuer_mode = "per_provider"`, and the same three `openid`/`email`/`profile`
`property_mappings` — plus a `kubernetes_secret` in the `authentik` namespace with
Reflector annotations scoping the mirror to the consuming app's namespace. What
varies per app: the redirect URI (`allowed_redirect_uris`, strict-matched to the
app's OIDC callback path) and the mirrored secret's key/env-var shape, since each
app expects its own config format. Compare:

- `provider_grafana.tf` — redirect `https://grafana.allegedly.works/login/generic_oauth`;
  secret keys `GF_AUTH_GENERIC_OAUTH_CLIENT_{ID,SECRET}`.
- `provider_headlamp.tf` — redirect `https://headlamp.allegedly.works/oidc-callback`;
  secret keys `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_ISSUER_URL`/`OIDC_SCOPES`.
- `provider_props.tf` — redirect `https://props.allegedly.works/auth/callback`;
  secret keys `client-id`/`client-secret` plus an app-specific
  `random_password`-generated `session-secret`, and an `authentik_policy_binding`
  gating login to `data.authentik_group.admins`.

### Blueprint-managed providers (deprecated)

Vault was decommissioned 2026-04-19 (see <../archive/2026_04_19_vault_migration.md>) and all SSO
providers moved to `tf/gitops/sso-providers/`. The `k8s/authentik/app/blueprints/`
flow no longer pulls client secrets from Vault. If you see a `!Env`-tagged client
secret in a blueprint, treat it as a bug to migrate, not a pattern to copy.

## Proxy-mode NetworkPolicy (required)

When a service is behind the shared proxy outpost, add a `networkpolicy.yaml` restricting
ingress to the outpost pod. Without this, any pod can forge `X-authentik-username` headers.

Real example: `k8s/scanner/networkpolicy.yaml`

Template:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: <service>-ingress
  namespace: <namespace>
spec:
  podSelector:
    matchLabels:
      <pod-label>: <value>
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: authentik
          podSelector:
            matchLabels:
              app.kubernetes.io/name: authentik
              app.kubernetes.io/component: server
      ports:
        - port: <backend-port>
          protocol: TCP
```

`namespaceSelector` + `podSelector` in the same `from` item are ANDed.

## Deleting Authentik providers or applications

Always add a `state: absent` tombstone entry — never just remove the `state: present`
block. The worker re-applies blueprints every 60 min; the absent entry is what actually
removes the stale resource. Follow the `CLEANUP` tombstone convention from <../../STYLE.md>.
Place absent entries in the app's existing blueprint, or in a dedicated cleanup blueprint
under `k8s/authentik/app/blueprints/` when the app itself is gone.
Remove the entries after a few reconcile cycles once confirmed clean.

**Exception**: When migrating a provider from blueprints to TF, use tombstones to delete
the old blueprint-managed resource, then let TF create a fresh one. The tombstone
and TF creation can coexist in the same commit.
