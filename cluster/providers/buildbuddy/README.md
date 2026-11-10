# BuildBuddy Crossplane provider

This provider manages BuildBuddy organization secrets from Kubernetes Secrets. It
uses Crossplane's managed reconciler, controller-runtime, controller-gen and
angryjet. Bazel generates the Kubernetes DeepCopy methods, Crossplane interface
methods and CRDs from the API types; generated Go code is not checked in. The
image contains both the controller and its Crossplane package metadata.
It calls BuildBuddy's gRPC API using the repository's generated protobuf bindings.

The API types, registration and ProviderConfig controller are adapted from the
[Crossplane provider template](https://github.com/crossplane/provider-template/tree/d3efb9cfe9c1a61903d2ac2c5b402258e7e4d55b),
licensed under [Apache 2.0](LICENSE-APACHE-2.0).

`Secret` is namespaced. Its `crossplane.io/external-name` annotation is the
BuildBuddy environment variable name; `spec.forProvider.valueSecretRef` selects a
nonempty key from a Kubernetes Secret in the same namespace. A ProviderConfig
selects an API key. Secret-backed provider credentials must also reside in the
managed resource's namespace. The controller watches the configured source
Secret and checks BuildBuddy on its one-minute poll. A source Secret revision,
public-key rotation, or absent remote name causes a write; it also reasserts
the value hourly. Each write is encrypted with BuildBuddy's public key.

BuildBuddy does not let this controller read back the plaintext. Ready means the
last write succeeded for the observed source revision and public-key ID, not that
the current remote plaintext has been independently verified. Status records
only the source UID, resource version, key name, external name, public-key ID,
and last successful write time. Neither the value nor its hash is stored there.
Missing input or credentials is an error; it never causes a remote deletion.
Choose management policies without `Delete` to retain a remote secret when its
managed resource is deleted. Changing the external name does not remove the
old remote secret.

The runtime requires `WATCH_NAMESPACE`, `WATCH_SECRET`, and `CREDENTIAL_SECRET`.
It watches only the configured source, rejects other source names, and accepts
only the configured credential Secret in the same namespace. Crossplane's RBAC
manager nevertheless grants provider pods cluster-wide access to Secrets and
other core resources; these checks limit what a managed resource can request,
not what a compromised provider process can access.

## LiteLLM ownership handoff

The cluster manages `litellm/live-tests` with `Observe`, `Create`, and `Update`
policies. Its former Terraform resource had a destroy provisioner that deletes
BuildBuddy `OPENAI_API_KEY`; it was forgotten from state using a `removed` block
with `destroy = false`. The remaining Terraform CR is suspended and can be
retired in a later change after checking deletion behavior and Flux ownership.
The BuildBuddy admin credential remains independently owned.
