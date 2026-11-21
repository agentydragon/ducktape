# SSO Integration (Authentik)

## Two provider management patterns

### TF-managed providers (preferred)

`tf/gitops/sso-providers/` and app-specific GitOps roots create Authentik providers
directly. Terraform owns generated client-secret lifecycles and the corresponding
applications, access policies, and consumer Secrets.

**Secret flow**: TF creates provider → reads `client_secret` → writes `kubernetes_secret`
in `authentik` namespace → Reflector mirrors to consumer namespace(s).

There are two current provider sources of truth: search Terraform for
`authentik_provider_{oauth2,proxy}` and read the blueprints in
`k8s/authentik/app/blueprints/` (every file there is deployed). Do not hand-maintain an application enumeration
here; ownership is still being consolidated under
<https://github.com/agentydragon/ducktape/issues/987>.

Most OIDC providers are map entries in `providers.tf` or `providers_confidential.tf`, which
hold the client ID and redirect URIs; a `provider_<app>.tf` normally adds the
`authentik_application` and its access policy. The consumer `kubernetes_secret` in the
`authentik` namespace, with Reflector annotations scoped to the consumer, is either an
entry of `reflected_oidc_secrets.tf` or written by the app's own file. Redirect URIs and
Secret keys remain app-specific. Compare:

- `grafana` — redirect `https://grafana.allegedly.works/login/generic_oauth`
  (`providers_confidential.tf`); secret keys `GF_AUTH_GENERIC_OAUTH_CLIENT_{ID,SECRET}`
  (`reflected_oidc_secrets.tf`).
- `headlamp` — redirect `https://headlamp.allegedly.works/oidc-callback` (`providers.tf`);
  secret keys `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_ISSUER_URL`/`OIDC_SCOPES`
  (`provider_headlamp.tf`).
- `props` — redirect `https://props.allegedly.works/auth/callback`
  (`providers_confidential.tf`); secret keys `client-id`/`client-secret` plus an
  app-specific `random_password`-generated `session-secret` (`reflected_oidc_secrets.tf`),
  and an `authentik_policy_binding` in `provider_props.tf` gating login to
  `data.authentik_group.admins`.

**Gotcha — give every `allowed_redirect_uris` entry a `redirect_uri_type`.** Authentik
defaults an omitted type to `authorization`; provider 2026.8.0 stores that on read and
diffs it against the omitted key, so the plan never converges and every 15-minute
reconcile PUTs the provider.

**Gotcha — `authentik_rbac_permission_role` returned `405 Method Not Allowed` on Authentik
2026.2.1.** Upstream never routed the `assign`/`unassign` actions of
`RoleAssignedPermissionViewSet`, so `POST /api/v3/rbac/permissions/assigned_by_roles/{uuid}/assign/`
failed in every release checked and on `master` (2026-05-05). Server and provider are now
2026.8: re-test before using the resource. The retired `claude-diagnostics` role (#11887) set
its permissions in an `authentik_rbac.role` blueprint entry instead, which avoids that endpoint.

### Blueprint-managed providers (migration backlog)

Active proxy providers remain under `k8s/authentik/app/blueprints/` while issue #987
migrates their ownership. These backends still require identity-aware proxy
authentication; moving them to Terraform must preserve `authentik_provider_proxy`, the
application policy, embedded-outpost membership, and the backend NetworkPolicy.

Vault was decommissioned 2026-04-19 (see <decisions.md> § "Secrets: SOPS
SSOT"). No active blueprint consumes a provider
client secret through `!Env`; reintroducing that pattern would restore the rotation-drift
bug this migration fixed.

## Proxy-mode NetworkPolicy (required)

When a service is behind the shared proxy outpost, add a `networkpolicy.yaml` restricting
ingress to the outpost pod. Without this, any pod can forge `X-authentik-username` headers.

Real example: `cdk8s/goldilocks.py`

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

When migrating a provider from blueprints to Terraform, tombstone only objects that the
new Terraform resources do not own. If the application slug survives while its protocol
provider changes, do not tombstone the application: deleting by slug would also delete
the live Terraform-owned application. The completed Kagent proxy-to-OIDC migration is the
reference case in <../archive/2026_07_kagent/docs/kagent_sso.md>.
