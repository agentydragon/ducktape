# Crossplane OpenTofu Workspace canary

This canary installs Upbound's `provider-opentofu` and adds an observe-only Crossplane
`Workspace` for `tf/gitops/forgejo-agentydragon`. The root manages four SSH keys on the
existing Forgejo `agentydragon` account. The existing tofu-controller `Terraform` remains
the only writer and keeps applying the same root.

The Crossplane Workspace uses the existing `forgejo_agentydragon` Postgres state schema and
the `default` OpenTofu workspace. This checks whether provider-opentofu can initialize against
state already written by tofu-controller and observe the root without importing or moving state.
Its `managementPolicies: [Observe]` prevents Crossplane from creating, updating, or deleting
managed infrastructure; `deletionPolicy: Orphan` and the non-pruning Flux Kustomization are
additional safeguards. The Workspace Flux unit waits for the provider, state database, and
existing Forgejo GitOps unit, but does not wait for the observer's readiness.

The source uses provider-opentofu's remote module support with
`git::https://github.com/agentydragon/ducktape.git//tf/gitops/forgejo-agentydragon?ref=devel`.
The provider polls the Workspace and `remotePullPolicy: Always` fetches the module on each
reconciliation, so a merged edit is noticed on the provider's next poll. Flux ArtifactGenerator
artifacts are not consumed by this Workspace: artifact-native delivery and event-driven refresh
remain open follow-ups. This canary also does not demonstrate per-root Kubernetes RBAC. The
Kubernetes provider and backend credential reference run through provider-opentofu's controller
identity, which differs from tofu-controller's `tf-runner` service account.

After the canary reports a successful observation against the existing state, evaluate whether
tofu-controller can remain the only writer while Crossplane observes, whether the provider's
Kubernetes identity can be narrowly scoped to the secret read this root requires, and what
source bridge can provide per-root artifact isolation. Do not remove the existing Terraform
resource or grant Crossplane `Create`, `Update`, or `Delete` as part of this spike.
