# Gitea Stack Helm Chart

This chart packages the full Ducktape Gitea deployment: the upstream Gitea Helm chart, Authentik OAuth bootstrap, Ember PAT automation, and Reflector for cross-namespace secret sharing.

## Prerequisites

- Sealed Secrets controller installed (Helmfile release `sealed-secrets`, Bitnami chart)
- Authentik chart deployed (`k8s/helm/authentik/`) so the Gitea blueprint exists
- A sealed secret for OAuth credentials and Ember bootstrap password (instructions below)

## Usage

```bash
cd k8s/helm/gitea
helm dependency update
helm upgrade --install gitea . \
  --namespace gitea \
  --create-namespace \
  --wait \
  --timeout 10m
```

All supporting jobs, RBAC, and secrets are rendered from this chart; no additional `kubectl apply` step is required.

## Bootstrapping Secrets

#### 1. Generate OAuth2 Credentials

```bash
OAUTH_CLIENT_ID="gitea-oauth2-client"
OAUTH_SECRET=$(openssl rand -hex 32)

kubectl create secret generic gitea-oauth-shared \
  --namespace=gitea \
  --from-literal=client_id="${OAUTH_CLIENT_ID}" \
  --from-literal=client_secret="${OAUTH_SECRET}" \
  --from-literal=GITEA_CLIENT_SECRET="${OAUTH_SECRET}" \
  --dry-run=client -o yaml | \
yq '.metadata.annotations["reflector.v1.k8s.emberstack.com/reflection-allowed"] = "true" |
    .metadata.annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] = "authentik"' | \
kubeseal --format yaml
```

Paste the resulting ciphertext into `values.yaml` under `.Values.sealedSecrets.oauth.encryptedData`. Save the plaintext client ID and secret somewhere safe—they are needed later when Authentik is rotated.

#### 2. Generate Ember Gitea Password

```bash
EMBER_PASSWORD=$(openssl rand -base64 24)

kubectl create secret generic gitea-ember-credentials \
  --namespace=gitea \
  --from-literal=ember-password="${EMBER_PASSWORD}" \
  --dry-run=client -o yaml | kubeseal --format yaml
```

Copy the encrypted payload into `.Values.sealedSecrets.emberCredentials.encryptedData`. This secret is consumed by the Ember PAT bootstrap job.

## Chart Components

- **Upstream Gitea chart** – deployed with the values under `.Values.gitea`.
- **reflector** – lightweight deployment installed automatically so secrets can reflect into the Authentik namespace.
- **OAuth setup job** – post-install hook that registers the Authentik OIDC provider in Gitea.
- **Ember PAT job** – creates/updates the `gitea-ember-token` secret in the `ember` namespace using the helper script in `files/ember_pat.py`.
- **Sealed secrets** – rendered via `common.sealedSecret`, keeping ciphertext in Git.
- **RBAC** – service account and cross-namespace permissions for the Ember secret writer.

## Operations

- Admin account: `agentydragon` (password stored in secret `gitea-admin-secret`).
- PAT output: secret `gitea-ember-token` in namespace `ember` (`username`, `token`, `token_name` keys).
- OAuth secret reflection: `gitea-oauth-shared` exists in both `gitea` and `authentik` namespaces via reflector.

## Troubleshooting

```bash
# Check OAuth bootstrap job
kubectl logs -n gitea job/gitea-oauth-setup

# Inspect Ember PAT bootstrap
kubectl logs -n gitea job/gitea-ember-bootstrap

# Verify reflected secrets
kubectl get secret -n authentik gitea-oauth-shared
kubectl get secret -n ember gitea-ember-token
```
