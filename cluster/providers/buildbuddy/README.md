# BuildBuddy Crossplane provider

This provider manages BuildBuddy organization secrets and group API keys. It
uses Crossplane's managed reconciler, controller-runtime, controller-gen and
angryjet. Bazel generates the Kubernetes DeepCopy methods, Crossplane interface
methods and CRDs from the API types; generated Go code is not checked in. The
image contains both the controller and its Crossplane package metadata. It calls
BuildBuddy's gRPC API using the repository's generated protobuf bindings.

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

`APIKey` manages only group-owned keys in an explicit `spec.forProvider.groupID`.
The configured BuildBuddy credential must be an organization admin for that
group so the list operation can observe every key and Create/Update/Delete can
manage them. `label`, an explicit `capabilities` list (which may be empty for a
key with no capabilities), and `visibleToDevelopers` control the key's mutable
metadata. The group ID is
immutable. Expiration is not exposed because BuildBuddy's list API omits it and
the update API cannot change it.

The provider requires BuildBuddy's `GetApiKey` value readback to be enabled.
Crossplane publishes the value only to the same-namespace
`spec.writeConnectionSecretToRef`, under `api-key`; it never stores the value in
the managed resource spec or status. The provider reads it back on each
observation, so it restores a missing or changed connection Secret. If readback
is disabled or denied, reconciliation reports an error. Change permissions or
labels in place; rotate a token by creating a replacement `APIKey`, moving
consumers, then deleting the old resource.

BuildBuddy's Create API has no idempotency token and does not accept a
caller-selected key ID. Crossplane persists an external-create-pending marker
before the RPC, then persists the returned ID before publishing the value. If
the provider crashes before it saves the ID, the pending marker prevents an
automatic duplicate create. Inspect the group's keys and resolve the marker:
if the key can be identified, set its ID as the external name and clear the
pending annotation; otherwise clear the marker only after confirming no key
was committed. If the ID was saved but Secret publication did not finish,
the next observation reads the value and publishes it.

If Create fails, the provider treats the outcome as uncertain, persists an
external-name marker, and blocks further key operations. This includes errors
such as permission denials. Inspect the group's keys before clearing it; if the
created key can be identified, set the external name to its ID and the provider
will read its value. Default `Delete`
management policy revokes a managed key; without `Delete`, Kubernetes can
garbage-collect the connection Secret while the remote key remains active.

The runtime requires `WATCH_NAMESPACE`, `WATCH_SECRET`, and `CREDENTIAL_SECRET`.
It watches only the configured source, rejects other source names, and accepts
only the configured credential Secret in the same namespace. Crossplane's RBAC
manager nevertheless grants provider pods cluster-wide access to Secrets and
other core resources; these checks limit what a managed resource can request,
not what a compromised provider process can access.

## LiteLLM ownership handoff

The cluster manages `litellm/live-tests` with `Observe`, `Create`, and `Update`
policies. The former `litellm-buildbuddy-sync` Terraform state was emptied using
a `removed` block with `destroy = false`. Both retired state schemas were then
removed without running their former destroy provisioners. The BuildBuddy admin
credential remains independently owned.
