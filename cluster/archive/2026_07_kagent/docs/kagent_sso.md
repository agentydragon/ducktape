# Kagent SSO

Kagent uses the upstream chart's `oauth2-proxy` integration, not an Authentik
forward-proxy outpost.

Kagent was parked and then archived: no workload or HTTPRoute remained in the cluster.
The configuration below records the final design rather than active ownership.

## Retained ownership and request path

```text
Browser
  -> cluster Gateway
  -> kagent-oauth2-proxy:4180
  -> kagent-ui:8080
```

- `../terraform/provider_kagent.tf` owned the Authentik OIDC provider,
  `kagent` application, admin policy binding, oauth2-proxy cookie secret, and reflected
  Kubernetes Secret.
- `../k8s/kagent/app/helmrelease.yaml` enabled the chart's oauth2-proxy and
  points it at Authentik's per-provider issuer.
- `../k8s/kagent/app/httproute.yaml` routed the public hostname directly to
  `kagent-oauth2-proxy`; there is no Authentik outpost in the request path.
- `../k8s/kagent/app/flux-kustomization.yaml` depended on
  `sso-providers-tf`, so the reflected Secret exists before the chart reconciles.

Authentik does not set `email_verified` for locally managed users. The deployment
therefore sets oauth2-proxy's `insecure-oidc-allow-unverified-email`; Authentik remains
the authority for whether the local email identity is acceptable.

## Legacy outpost retirement

The previous `kagent` proxy provider and standalone `kagent-outpost` were retired on
2026-07-21. Authentik and Kubernetes were checked after reconciliation: neither legacy
Authentik object nor any outpost Deployment, Pod, or Service remained.

During proxy retirement, the Terraform-owned OIDC application deliberately retained the
`kagent` slug. An application tombstone would have removed that replacement regardless of
which declaration created it, so the migration tombstoned only the legacy proxy provider
and outpost. The temporary cleanup blueprint was removed after both were verified absent;
the unused OIDC application was retired separately when Kagent itself was archived.
