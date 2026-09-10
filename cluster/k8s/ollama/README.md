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
to update an existing Secret without deleting it. Initializing an empty backend
generates a new token and takes ownership of that field.
Keep `data` and `stringData` out of the Flux manifest so Flux does not clear the token.

Reflector copies the token to `claude-sandbox`. Reloader restarts Ollama when it
changes because nginx reads the token from an environment variable. The Deployment
uses `Recreate`, so rotation includes a service interruption during that restart.
Clients that cached the previous bearer token must fetch the new one.
