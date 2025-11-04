# Vault / External Secrets Progress - 2025-10-31

## Summary
- Split monolithic `authentik`, `vault`, and `external-secrets` Helm values into focused overlays for core app config, persistence, blueprints, and secrets.
- Regenerated SealedSecret ciphertexts for Vault OIDC client credentials and the Vault root token via `kubeseal`, wiring the new values into the Helm charts.
- Adjusted the wrapper charts and Helmfile release definitions to consume the new overlays and ensured the Vault chart renders valid manifests with TLS-configured raft listeners.
- Updated the External Secrets wrapper to enable CRD installation so `ClusterSecretStore` resources can be created ahead of the Vault deployment.

## Progress
- `authentik` helmfile apply succeeded with the refactored values and fresh secrets.
- `external-secrets` chart now installs CRDs (`installCRDs: true`), resolving the missing `ClusterSecretStore` API that blocked Vault deployment.
- Vault chart templates render cleanly after correcting extra volume handling and listener TLS paths; helmfile apply currently reruns after CRD rollout.

## Next Steps
1. Re-run `helmfile apply -l name=external-secrets --skip-diff-on-install` to ensure the operator and CRDs install successfully in the cluster.
2. Execute `helmfile apply -l name=vault -i --skip-diff-on-install` to deploy Vault once the CRDs are present.
3. Verify reflected secrets (Vault OIDC credentials and root token) materialize in the `vault` namespace and confirm the OIDC bootstrap job succeeds.
