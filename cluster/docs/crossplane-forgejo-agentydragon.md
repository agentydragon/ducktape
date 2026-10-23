# Crossplane OpenTofu ownership spike

This spike moves `tf/gitops/forgejo-agentydragon` from tofu-controller to Upbound's
`provider-opentofu`. That root manages the four SSH public keys on the Forgejo
`agentydragon` account.

The Crossplane `Workspace` uses the same `forgejo_agentydragon` PostgreSQL schema and
`default` OpenTofu workspace as the existing tofu-controller `Terraform` object. It does
not import or copy state. The existing root is removed from the `forgejo-gitops` bundle;
tofu-controller's CRD defaults `destroyResourcesOnDeletion` to `false`, so pruning that CR
leaves both the Forgejo keys and remote state in place. The Crossplane Kustomization depends
on `forgejo-gitops`, so it starts only after Flux has completed that handoff. The two
controllers therefore do not reconcile the same state at once.

Crossplane manages `Observe`, `Create`, and `Update`. OpenTofu can create, change, and remove
resources as the HCL changes. `Delete` is omitted and the Workspace has
`deletionPolicy: Orphan`, so deleting the Crossplane object will leave the Forgejo keys and
backend state intact for recovery or a later owner.

The Workspace uses provider-opentofu's remote Git module source:
`git::https://github.com/agentydragon/ducktape.git//tf/gitops/forgejo-agentydragon?ref=devel`.
`remotePullPolicy: Always` refreshes the source during the provider's polling reconciliation;
this spike does not deliver the Flux `ArtifactGenerator` artifact directly and source changes
are not event-triggered. The root's Kubernetes Secret data source also runs under the
provider's Kubernetes identity, not tofu-controller's `tf-runner` service account, so the
provider's actual Secret access is part of the live validation.

After merge, verify that `forgejo-gitops` becomes Ready after pruning the Terraform CR, then
that `crossplane-forgejo-agentydragon` becomes Ready while observing the four existing keys
from the existing state. Confirm the provider's first plan is a no-op and subsequent edits
to this root reconcile through Crossplane. That establishes the operational ownership move;
artifact-native source delivery and per-root RBAC remain follow-up work for evaluating a
broader tofu-controller migration.
