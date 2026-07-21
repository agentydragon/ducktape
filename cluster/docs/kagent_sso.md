# Kagent SSO

Kagent uses the upstream chart's `oauth2-proxy` integration, not an Authentik
forward-proxy outpost.

Kagent is currently parked: its Flux Kustomizations are suspended and no workload or
HTTPRoute is present in the cluster. The configuration below is retained for a future
resume.

## Retained ownership and request path

```text
Browser
  -> cluster Gateway
  -> kagent-oauth2-proxy:4180
  -> kagent-ui:8080
```

- `tf/gitops/sso-providers/provider_kagent.tf` owns the Authentik OIDC provider,
  `kagent` application, admin policy binding, oauth2-proxy cookie secret, and reflected
  Kubernetes Secret.
- `cluster/k8s/agents/kagent/app/helmrelease.yaml` enables the chart's oauth2-proxy and
  points it at Authentik's per-provider issuer.
- `cluster/k8s/agents/kagent/app/httproute.yaml` routes the public hostname directly to
  `kagent-oauth2-proxy`; there is no Authentik outpost in the request path.
- `cluster/k8s/agents/kagent/app/flux-kustomization.yaml` depends on
  `sso-providers-tf`, so the reflected Secret exists before the chart reconciles.

Authentik does not set `email_verified` for locally managed users. The deployment
therefore sets oauth2-proxy's `insecure-oidc-allow-unverified-email`; Authentik remains
the authority for whether the local email identity is acceptable.

## Legacy outpost retirement

The previous `kagent` proxy provider and standalone `kagent-outpost` are obsolete.
`cluster/k8s/authentik/app/blueprints/kagent-sso.yaml` is a temporary `state: absent`
tombstone for those two objects.

The live Terraform-owned OIDC application deliberately retains the `kagent` slug. Never
add an application tombstone for that slug: Authentik deletion resolves by object identity
and would remove the live application regardless of which declaration originally created
it. Delete the cleanup blueprint only after both the legacy proxy provider and
`kagent-outpost` are absent from Authentik.
