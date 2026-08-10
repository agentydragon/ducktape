# External credential distribution

This is the cluster-wide pattern for static credentials for services operated
outside the cluster. The initial migrations are LLM provider keys, but the
pattern also applies to other eligible API credentials. Canonical SOPS Secrets
live in `ducktape-flux`. Consumers receive a local copy only through a
dedicated edge in `distribution.yaml`: a target-namespace ServiceAccount
authenticates a local `SecretStore`, while a `ducktape-flux` Role permits that
identity to `get` only the one named source Secret.

Add a credential by creating one encrypted source Secret and one source Role.
Add a consumer by adding a target ServiceAccount, RoleBinding, SecretStore, and
an `ExternalSecret` in the consuming component. Do not use a shared
ClusterSecretStore for these credentials: it would grant every allowed target
namespace access to every source Secret.

This pattern excludes cluster-internal credentials, Kubernetes-native identity
and PKI material, and credentials minted or rotated by a controller. OAuth and
session credentials (for example Tana and CLIProxyAPI) remain explicit
exceptions. Normal workloads use scoped LiteLLM virtual keys instead of vendor
credentials where that is available.
