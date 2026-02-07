# Matrix Stack (Synapse + Element)

This chart is now a thin wrapper around the upstream [`matrix`](https://github.com/remram44/matrix-helm) Helm chart. The dependency ships Synapse, Element, and (optionally) PostgreSQL. Our wrapper keeps the declarative bits we rely on:

- Traefik-friendly ingress values that serve Element at `/` and Synapse APIs under `/_matrix`, `/_synapse`, and `/.well-known` on the same host.
- Bootstrap Jobs for the Synapse admin account and the Ember bot (injecting the registration-shared secret flow, passwords, and delivering the Ember access token into `ember` namespace).
- Secrets delivered through SealedSecrets so we never store cleartext credentials in git.
- A single values file (`values.yaml`) that configures Synapse OIDC, Element, and the bootstrap helpers.

## Key values

All defaults live in `values.yaml` and can be overridden in your own file:

- `matrix.*` — forwarded directly to the upstream chart. Important entries we set by default:
  - `matrix.homeserverConfig.server_name`, `public_baseurl`, `web_client_location`.
  - `matrix.homeserverConfig.oidc_providers` describing the Authentik IdP (client secret is supplied separately via `matrix.extraConfig`).
  - `matrix.extraConfig[0]` injects the OIDC client secret from the bootstrap secret without committing it to git (keep the secret name in sync with `bootstrap.secret.name` if you customise it).
  - `matrix.ingress` and `matrix.element.ingress` expose both services on `https://matrix.k3s.agentydragon.com/` using Traefik.
- `bootstrap.secret.*` — name of the secret (defaults to `matrix-stack-bootstrap`) plus the base64-encoded values used when `sealedSecrets.enabled: false`.
- `bootstrap.admin.*` — admin username and the key inside the bootstrap secret that contains its password.
- `bootstrap.ember.*` — Ember bot username, target namespace, destination secret name for the token, and password key inside the bootstrap secret.
- `sealedSecrets.*` — enablement flag and the encrypted data for `admin-password`, `ember-password`, and `oidc-client-secret`. When enabled (default), the template renders a `SealedSecret` named `matrix-stack-bootstrap` which the controller then unwraps into the runtime secret that both the bootstrap Jobs and Synapse consume.

## Sealed secret workflow

If you need to regenerate the bootstrap credentials:

```bash
kubectl create secret generic matrix-stack-bootstrap \
  --from-literal=admin-password='<admin-password>' \
  --from-literal=ember-password='<ember-password>' \
  --from-literal=oidc-client-secret='<authentik-client-secret>' \
  --dry-run=client -o yaml > /tmp/matrix-stack-bootstrap.yaml

kubeseal --format yaml \
  --name matrix-stack-bootstrap \
  --namespace matrix \
  < /tmp/matrix-stack-bootstrap.yaml > sealed-matrix-stack-bootstrap.yaml
```

Copy the resulting `encryptedData` values into `values.yaml` under `sealedSecrets.encryptedData`.

## Authentik SSO setup

- Configure an Authentik OAuth2 provider with redirect URI `https://matrix.k3s.agentydragon.com/_synapse/client/oidc/callback`.
- Place the issuer URL and client ID in `matrix.homeserverConfig.oidc_providers[0]`.
- Seal the client secret into the bootstrap secret; it is injected at runtime via `matrix.extraConfig` so it never appears in plain text in the chart.
- Enforce membership in the `matrix-users` group via `claim_requirements` (already part of the default values).
- PKCE is required (`pkce_method: always`) which lines up with Authentik advertising `S256`.

## Install / upgrade

```bash
helm dependency update k8s/helm/matrix-stack
helm upgrade --install matrix k8s/helm/matrix-stack \
  -n matrix --create-namespace \
  -f your-values.yaml
```

The upstream chart handles Synapse, Element, and (if enabled) PostgreSQL. Once those pods are up, our bootstrap Jobs will:

1. Create or confirm the admin user (password pulled from the bootstrap secret).
2. Provision the Ember bot, login, and write the access token into the `ember` namespace secret referenced by your controllers.

## Element

Element is served from `/` on `matrix.k3s.agentydragon.com`, matching the upstream chart defaults. No more `/element` path rewrites or Traefik CRDs are required.

## Local development

If you want a non-Kubernetes setup for experimentation, see the `matrix/` directory in the repo for docker-compose files and helper scripts.
