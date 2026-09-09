# Ollama bearer token

`secrets/ollama-bearer-token-tf.yaml` runs `tf/gitops/ollama-bearer-token` in
`ollama`, with automatic plan approval. Its `tf-runner` ServiceAccount has a
namespace-scoped RoleBinding to the controller's runner role. Do not add it to
the Helm chart's `runner.serviceAccount.allowedNamespaces`: that chart creates
cluster-wide bindings for those accounts.

The PostgreSQL backend uses the dedicated `tfstate_ollama_bearer_token` database
and login on `tofu-state-db-ovh`, with TLS. Only its password is reflected into
`ollama`; see [state database ownership and checks](../tofu-state/README.md).

Flux owns the `ollama-bearer-token` Secret object and reflection annotations.
Terraform's `kubernetes_secret_v1_data` owns `data.token`, using server-side apply
to update an existing Secret without deleting it. Fresh backend state generates a
new token and takes ownership of that field. No import or old state copy is needed.
Keep `data` and `stringData` out of the Flux manifest so Flux does not clear the token.

Reflector copies the token to `claude-sandbox`. Reloader restarts Ollama when it
changes because nginx reads the token from an environment variable. The Deployment
uses `Recreate`, so rotation includes a service interruption during that restart.
Clients that cached the previous bearer token must fetch the new one.

## One-time GitOps cutover from shared state

The cutover uses separate retirement and activation revisions. Complete the
[database connection checks](../tofu-state/README.md) before either stage.

1. Land the retirement PR, which removes the old `flux-system/ollama-bearer-token`
   manifest, resource entry and health check. Its Terraform root stays unchanged.
   Flux prunes the old CR; `destroyResourcesOnDeletion: false` preserves the token
   and state. Leave the CR unsuspended so tofu-controller can run its finalizer.
2. Wait for the retirement revision to reconcile. Confirm both the old Terraform
   and its runner are absent, and the existing token Secret is preserved. Flux
   readiness alone is insufficient because pruning can finish asynchronously.

   ```bash
   kubectl wait --for=delete terraform/ollama-bearer-token -n flux-system --timeout=10m
   kubectl wait --for=delete pod/ollama-bearer-token-tf-runner -n flux-system --timeout=10m
   ```

   If either resource is already absent, its wait is satisfied. If a runner remains,
   investigate before proceeding; do not forcibly delete an active apply.

3. Land the activation PR (#5957) only after retirement is verified. It introduces
   the new CR in `ollama` with fresh state and automatic plan approval. Flux creates
   or adopts the token Secret's metadata, then Terraform replaces its token in place.
4. Verify the new `ollama/ollama-bearer-token` is Ready, its plan applied, and state
   was created in the dedicated database. Check that `ollama/tf-runner` cannot read
   Secrets in `flux-system` or `tofu-state`. Verify the bearer changed and its
   reflected copy matches without printing either value.
5. Wait for the Ollama rollout, then call the authenticated `/api/tags` endpoint
   with the new token (expect success), the previous token (expect rejection), and
   no token (expect rejection). Confirm the next Terraform reconciliation has no
   changes. A healthy CR or updated Secret alone does not establish service success.

Keep the retirement and activation stages separate: changing the Terraform resource
model while the old writer still exists could make it plan against the old state.
An in-flight apply may finish during retirement; the new writer starts only after
that runner has gone.

The old `tfstate.ollama_bearer_token` state is intentionally not migrated or reused.
It records the retired token. Keep the old CR absent: applying that state could
restore the retired token, and destroying it could delete the live Secret. Remove
the abandoned state separately after successful verification; do not run destroy.
