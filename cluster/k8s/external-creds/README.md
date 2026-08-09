# LLM credential distribution

External LLM credentials are canonical SOPS Secrets in `ducktape-flux`.
Consumers receive a local copy only through a dedicated edge in
`distribution.yaml`: a target-namespace ServiceAccount authenticates a local
`SecretStore`, while a `ducktape-flux` Role permits that identity to `get` only
the one named source Secret.

Add a credential by creating one encrypted source Secret and one source Role.
Add a consumer by adding a target ServiceAccount, RoleBinding, SecretStore, and
an `ExternalSecret` in the consuming component. Do not use a shared
ClusterSecretStore for these credentials: it would grant every allowed target
namespace access to every source Secret. Normal workloads use scoped LiteLLM
virtual keys instead of vendor credentials. OAuth/session credentials such as
Tana and CLIProxyAPI are intentionally outside this pattern.
