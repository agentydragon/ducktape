# BuildBuddy Crossplane provider

This provider manages BuildBuddy organization secrets from Kubernetes Secrets. It
uses Crossplane's managed reconciler, controller-runtime, controller-gen and
angryjet. Bazel generates the Kubernetes DeepCopy methods, Crossplane interface
methods and CRDs from the API types; generated Go code is not checked in. The
image contains both the controller and its Crossplane package metadata.

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

The cluster installs the provider and stages `litellm/live-tests` paused with
`Observe`, `Create`, and `Update` policies. Terraform remains the only active
writer of BuildBuddy `OPENAI_API_KEY`. The Terraform resource has a destroy
provisioner that deletes the remote secret, including when its replacement
triggers change. Do not remove the Terraform CR, its state, or its Flux owner
as part of enabling the staged resource.

A follow-up migration must first stop the Terraform writer and prove no runner
is in flight, then remove its state ownership without executing the destroy
provisioner. Only after verifying that Terraform cannot write or delete the
secret should it enable Crossplane and verify a successful write and live-test
consumer behavior. Preserve the independently owned BuildBuddy admin credential
through the transfer. Remove the old Terraform resources in a later change,
after checking their deletion behavior and Flux ownership. This PR does not
perform that handoff.
