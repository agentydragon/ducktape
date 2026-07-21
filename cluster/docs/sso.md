# SSO Integration (Authentik)

## Two provider management patterns

### TF-managed providers (preferred)

`tf/gitops/sso-providers/` and app-specific GitOps roots create Authentik providers
directly. Terraform owns generated client-secret lifecycles and the corresponding
applications, access policies, and consumer Secrets.

**Secret flow**: TF creates provider → reads `client_secret` → writes `kubernetes_secret`
in `authentik` namespace → Reflector mirrors to consumer namespace(s).

There are two current provider sources of truth: search Terraform for
`authentik_provider_{oauth2,proxy}` and read the blueprint file list in
`k8s/authentik/app/kustomization.yaml`. Do not hand-maintain an application enumeration
here; ownership is still being consolidated under
<https://github.com/agentydragon/ducktape/issues/987>.

OIDC provider files normally pair `authentik_provider_oauth2` with
`authentik_application` and write a `kubernetes_secret` in the `authentik` namespace
with Reflector annotations scoped to the consumer. Redirect URIs and Secret keys remain
app-specific. Compare:

- `provider_grafana.tf` — redirect `https://grafana.allegedly.works/login/generic_oauth`;
  secret keys `GF_AUTH_GENERIC_OAUTH_CLIENT_{ID,SECRET}`.
- `provider_headlamp.tf` — redirect `https://headlamp.allegedly.works/oidc-callback`;
  secret keys `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_ISSUER_URL`/`OIDC_SCOPES`.
- `provider_props.tf` — redirect `https://props.allegedly.works/auth/callback`;
  secret keys `client-id`/`client-secret` plus an app-specific
  `random_password`-generated `session-secret`, and an `authentik_policy_binding`
  gating login to `data.authentik_group.admins`.

### Blueprint-managed providers (migration backlog)

Active proxy providers remain under `k8s/authentik/app/blueprints/` while issue #987
migrates their ownership. These backends still require identity-aware proxy
authentication; moving them to Terraform must preserve `authentik_provider_proxy`, the
application policy, embedded-outpost membership, and the backend NetworkPolicy.

Vault was decommissioned 2026-04-19 (see
<../archive/2026_04_19_vault_migration.md>). No active blueprint consumes a provider
client secret through `!Env`; reintroducing that pattern would restore the rotation-drift
bug this migration fixed.

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

When migrating a provider from blueprints to Terraform, tombstone only objects that the
new Terraform resources do not own. If the application slug survives while its protocol
provider changes, do not tombstone the application: deleting by slug would also delete
the live Terraform-owned application. The completed Kagent proxy-to-OIDC migration is the
reference case in <../archive/2026_07_kagent/docs/kagent_sso.md>.
