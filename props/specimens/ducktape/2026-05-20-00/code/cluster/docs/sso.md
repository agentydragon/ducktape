# SSO Integration (Authentik)

## Two provider management patterns

### TF-managed providers (preferred for new providers)

`terraform/gitops/sso-providers/` creates `authentik_provider_oauth2` resources directly.
TF owns the client_secret lifecycle — no Vault, no ESO, no `!Env` drift.

**Secret flow**: TF creates provider → reads `client_secret` → writes `kubernetes_secret`
in `authentik` namespace → Reflector mirrors to consumer namespace(s).

**Currently managed**: grafana, headlamp, openclaw-agent.

### Blueprint-managed providers (deprecated)

Vault was decommissioned 2026-04-19 (see <../vault-migration/TODO.md>) and all SSO
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
(e.g., `k8s/authentik/blueprints/headscale-cleanup.yaml`) when the app itself is gone.
Remove the entries after a few reconcile cycles once confirmed clean.

**Exception**: When migrating a provider from blueprints to TF, use tombstones to delete
the old blueprint-managed resource, then let TF create a fresh one. The tombstone
and TF creation can coexist in the same commit.
