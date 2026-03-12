# Claude Hooks Secrets

Secrets are now managed via Kubernetes SealedSecrets in the cluster, replacing
the previous age-encrypted files.

## Architecture

1. `DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN` (SA token) is provided via Anthropic's env config
2. At session start, the hook reads secrets from k8s Secrets in the `claude-sandbox` namespace
3. Secret-to-env-var mapping is defined in `.claude_hooks/config.yaml`

## SealedSecrets

Secrets are stored as SealedSecrets in `cluster/k8s/claude-sandbox-secrets/`:

- `github-token-sealed.yaml` → `GITHUB_TOKEN`
- `buildbuddy-api-key-sealed.yaml` → `BUILDBUDDY_API_KEY`

To re-seal a secret:

```bash
kubectl create secret generic <name> --namespace=claude-sandbox \
  --from-literal=<key>=<value> --dry-run=client -o yaml | \
  ./cluster/scripts/seal-secret.sh /dev/stdin cluster/k8s/claude-sandbox-secrets/<name>-sealed.yaml
```

## Security

- The `claude-code-web` ServiceAccount has access to the `claude-sandbox` namespace
- Resource quotas limit what can be created in the sandbox
- SealedSecrets are encrypted with the cluster's stable RSA keypair
