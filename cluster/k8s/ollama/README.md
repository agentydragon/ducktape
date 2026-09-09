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

## One-time cutover from shared state

Land and verify the dedicated database prerequisite (#5955) and shared Git source
change (#5945) before this cutover. Retire the old writer **before merging the
cutover**, while the Git source still contains the old Terraform code:

1. Complete the database connection checks linked above. Confirm the dedicated
   credential Secret has been reflected into `ollama` and the dedicated database
   has no existing Terraform state. Keep the previous bearer token only in process
   memory for the later rejection check; do not print or persist it.
2. Suspend the owning Flux Kustomization and the old Terraform. Suspending the
   Terraform does not cancel an apply already in progress; wait for its runner to
   finish and disappear. Do not forcibly delete an active runner.

   ```bash
   flux suspend kustomization ollama-secrets -n ducktape-flux
   kubectl patch terraform ollama-bearer-token -n flux-system --type=merge \
     -p '{"spec":{"suspend":true,"destroyResourcesOnDeletion":false}}'
   kubectl wait --for=delete pod/ollama-bearer-token-tf-runner -n flux-system --timeout=10m
   ```

   If the pod is already absent, the wait is satisfied. If it remains, investigate
   before proceeding. Check that the controller is idle for this Terraform.

3. Delete the old CR without destroying its resources. In controller v0.16.5,
   suspension also blocks finalization: request deletion first, then resume the
   **already-deleting** CR so its finalizer can run.

   ```bash
   kubectl delete terraform ollama-bearer-token -n flux-system --wait=false
   kubectl patch terraform ollama-bearer-token -n flux-system --type=merge \
     -p '{"spec":{"suspend":false}}'
   kubectl wait --for=delete terraform/ollama-bearer-token -n flux-system --timeout=10m
   kubectl wait --for=delete pod/ollama-bearer-token-tf-runner -n flux-system --timeout=10m
   ```

   Confirm both the old CR and runner are gone. The token Secret and PostgreSQL
   state remain. Leave the Kustomization suspended until its source contains the
   cutover; resuming against the old source would recreate the old writer.

4. Merge the cutover, reconcile `ducktape-flux/ducktape`, and verify its artifact
   revision includes the merge. Resume `ducktape-flux/ollama-secrets`. Its Secret
   manifest preserves the current token until the new Terraform updates it.
5. Verify the new `ollama/ollama-bearer-token` is Ready, its plan applied, and state
   was created in the dedicated database. Check the new ServiceAccount cannot read
   Secrets in `flux-system` or `tofu-state`. Verify the bearer changed and its
   reflected copy matches, without printing either value.
6. Wait for the Ollama rollout, then call the authenticated `/api/tags` endpoint
   with the new token (expect success), the previous token (expect rejection), and
   no token (expect rejection). Confirm the next Terraform reconciliation has no
   changes. A healthy CR or updated Secret alone does not establish service success.

The old `tfstate.ollama_bearer_token` state is intentionally not migrated or reused.
It records the retired token. Keep the old CR absent: applying that state could
restore the retired token, and destroying it could delete the live Secret. Remove
the abandoned state separately after successful verification; do not run destroy.
