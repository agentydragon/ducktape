# Push-triggered Crossplane OpenTofu sources

## Decision: render Inline Workspaces with cdk8s

Use `Workspace.spec.forProvider.source: Inline` for roots whose configuration can be
assembled during cdk8s synthesis. The Workspace carries the desired OpenTofu source
itself. Flux applies a changed Workspace spec, and provider-opentofu reconciles it.
There is no need for an artifact-URL bridge, XRD, Composition, or function for this case.

The existing Crossplane OpenTofu roots are `tf/gitops/forgejo-agentydragon` and
`tf/gitops/route53-domain-delegation`. Both appear suitable for Inline. LiteLLM keys
are now native Crossplane LiteLLM resources, not OpenTofu Workspaces.

Both existing Workspaces render Inline HCL from their roots through cdk8s. The
`inline_source` filegroups package their `.tf` files as Bazel runfiles, so source edits
are inputs to manifest synthesis and its parity check.

## Source-change path

1. Edit a root's `.tf` files and regenerate the cdk8s manifests in the same change.
2. Existing CI checks the committed manifests against synthesis; no new publisher,
   pin-writing workflow, registry artifact, or bot commit is required.
3. Merge to `devel`. The existing GitHub Receiver wakes Flux's GitRepository.
4. The ArtifactGenerator updates the artifact containing that root's generated
   Kubernetes manifests. Flux applies the changed Inline Workspace.
5. Crossplane observes the Workspace spec change and runs OpenTofu reconciliation.

An unrelated push leaves the generated Workspace source unchanged. Do not include
repository commit SHAs or timestamps in its spec: those would cause unrelated edits
to change the desired configuration. Flux can still reapply resources during normal
reconciliation; unchanged desired source is not a new source version.

This path is driven by the existing webhook and Kubernetes watches, with Flux's
intervals as recovery for missed events. It does not require polling an artifact to
copy its URL into a Workspace.

Inline removes remote root downloads. It does **not** disable periodic OpenTofu
plans for drift detection. The pinned provider configures a managed-resource poll
interval, and `Observe` checks the OpenTofu diff. Apply happens when reconciliation
finds work to do. See the [v1.1.10 controller](https://github.com/upbound/provider-opentofu/blob/v1.1.10/internal/controller/cluster/workspace/workspace.go).

## Assembling Inline configuration

Use the generated cdk8s Workspace wrapper. Read declared Bazel runfiles and join the
root's ordinary HCL `.tf` files in a deterministic order with newline separators.
Keep the native Terraform files as the source of truth; do not hand-maintain a second
copy of the HCL in Python or YAML. Keep backend configuration, variables, credentials,
Workspace identity, external name, state schema, and management policies unchanged.

Multiple ordinary `.tf` files are one Terraform module, so Inline is not restricted
to single-file roots. The provider writes the module string as `main.tf`, or
`main.tf.json` with `inlineFormat: JSON`; see its [Workspace API](https://github.com/upbound/provider-opentofu/blob/v1.1.10/apis/cluster/v1beta1/workspace_types.go).

The assembly needs deliberate handling for:

- `.tf.json` mixed with HCL, and Terraform override files: simple concatenation does
  not preserve those file formats or override semantics.
- Local module trees and files read by `file()`, `templatefile()`, or `fileset()`:
  Inline supplies one configuration file, not a filesystem archive.
- Kubernetes object size limits for large roots.
- Flux substitution: preserve Terraform `${...}` expressions rather than treating
  them as Flux variables if a consumer enables `postBuild` substitution.

The current root survey found no actual local module calls or Terraform file-reading
functions. Forgejo's public keys are already embedded in HCL; Route 53's variables
are supplied by its Workspace. `gitlab-spike` mixes HCL with `versions.tf.json` and
needs a separate assembly decision. Remote remains an option when a root actually
needs a file tree; there is no demonstrated need for it in the two current Workspaces.

A Composition remains useful for a higher-level infrastructure API whose inputs
produce an Inline Workspace. It is unnecessary solely to trigger a directly
Flux-managed Workspace when checked-in configuration changes.

## Alternatives retained

| Option                                                          | How the Workspace changes                                                                             | Trade-off                                                                                                                               |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **cdk8s Inline + existing Flux Kustomization**                  | Generated manifest contains the changed HCL.                                                          | Recommended for the current roots. Requires regeneration alongside source edits; no remote root fetch or new controller.                |
| Immutable Git URL + `IfNotPresent`                              | A generator or publisher updates `module` to a relevant commit.                                       | Downloads the repository once per source version rather than every reconciliation; still needs a pin producer.                          |
| Root archive + `IfNotPresent`                                   | A publisher or bridge sets `module` to an immutable archive URL.                                      | Preserves files and local modules; requires publication and propagation of the URL.                                                     |
| OCI manifest artifact + Flux Kustomization                      | CI publishes manifests containing Inline HCL or an immutable Remote URL.                              | Existing controllers can apply it on a source update, but adds CI publication that we currently prefer to avoid.                        |
| OCI Helm chart + HelmRelease                                    | A chart carries the changed Workspace configuration.                                                  | Similar to OCI manifests, with Helm release ownership and more packaging.                                                               |
| CI writes a GitOps pin                                          | Bot commit or PR changes the Workspace source URL.                                                    | Auditable, but adds writeback credentials, sequencing, and loop avoidance.                                                              |
| Flux ExternalArtifact + provider-kubernetes watch + Composition | An observe-only `Object` watches artifact status; the Composition renders the Workspace with its URL. | Entirely in-cluster, but adds APIs, a function, alpha watch behavior, and broad list/watch access. Canary withdrawn in favor of Inline. |
| Dedicated artifact watcher                                      | Controller watches an artifact and patches a Workspace or pin ConfigMap.                              | Small behavior, but we own another controller's code and lifecycle.                                                                     |

For Remote sources, provider-opentofu v1.1.10 supports `remotePullPolicy: IfNotPresent`.
It reuses an initialized download when the URL matches `status.atProvider.remoteSource`
and the local lockfile is valid. A changed URL or lost local initialization requires a
new download. See the [pinned example](https://github.com/upbound/provider-opentofu/blob/v1.1.10/examples/cluster/workspace-remote-pull-policy-if-not-present.yaml).

A Flux artifact URL, an OCI image reference, and an OpenTofu module address are not
interchangeable. Any archive/OCI design needs to validate the exact address and format
with our pinned provider and confirm that its runtime can reach the source.

For OCI publication, Flux can consume an [OCIRepository](https://fluxcd.io/flux/components/source/ocirepositories/)
through a [Kustomization](https://fluxcd.io/flux/components/kustomize/kustomizations/#source-reference)
or [HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/#helm-chart-reference).
The existing Receiver would need to target that OCIRepository, and GHCR package events
need the package linked to the repository. Content-based publication should exclude
unrelated pushes. [Flux push artifact](https://fluxcd.io/flux/cmd/flux_push_artifact/)
provides the publishing mechanism.

### Affordances that do not supply the whole trigger

- Composition [required resources](https://docs.crossplane.io/latest/composition/compositions/#required-resources)
  fetch inputs during composition; fetching an artifact is not itself a watch subscription.
- Helm `lookup` reads live state when rendering; it does not arrange rendering when
  that state changes.
- A Flux Receiver wakes source reconciliation; it does not copy artifact status into
  a Workspace field.
- Flux `postBuild.substituteFrom` can react to a watched ConfigMap, but something still
  has to write the new pin into that ConfigMap.
- Flux ImageUpdateAutomation writes image references to Git, rather than providing a
  general artifact-status-to-Workspace bridge.
- A Flux-triggered Job can patch a Workspace, but still needs the source pin and write
  RBAC, and adds Job rerun/lifecycle concerns.
- Running OpenTofu in CI changes the execution and credential boundary; it does not
  make the in-cluster Crossplane Workspace source-driven.

## Withdrawing the artifact canary

The output-only `crossplane-source-update-canary` is being removed, including its
Terraform root, XR, XRD, Composition, root artifact, provider-kubernetes installation,
Composition function, runtime configuration, and dedicated artifact-reader RBAC.
The unmerged Go-template follow-up is closed.

First merge and reconcile the pruning-enablement PR: the provider installation's
Kustomization previously had `prune: false`. Then merge the stacked removal. Confirm
that the XR and composed Objects/Workspace are gone and package teardown completes.
The Workspace's orphan policy preserves its PostgreSQL state; do not delete the
shared state database or either live root's state. No canary-created external
infrastructure exists. The OpenTofu and LiteLLM providers remain installed.

After the separate Inline migration merges, verify both existing Workspaces report
`Synced=True` and `Ready=True`, retain their state identities, and expose the expected
non-sensitive outputs. A later harmless source edit can exercise the update path;
Route 53 registration and delegation must not be changed just to test triggering.

### One-time Remote-to-Inline cutover

provider-opentofu v1.1.10 writes Inline source to `main.tf` in the existing per-UID
working directory. It does not remove the old Remote root's other `.tf` files. An
existing warm runtime may therefore report duplicate declarations after the source
mode changes. Once Flux has applied **both** Inline Workspace specs, restart the
OpenTofu provider deployment to discard its ephemeral container filesystem, then
confirm both Workspaces reconcile from the retained PostgreSQL backend. The deployed
provider has no persistent volume mounted for `/tofu`. Do not delete or recreate the
Workspaces, change their external names, or remove backend state to clear this cache.

A restart before both spec changes is insufficient: a still-Remote Workspace could
repopulate its directory. This is a one-time migration step, not part of future
source-change delivery. A cold provider runtime writes the complete Inline module
without requiring a repository download.

## Kubernetes identities for migrated roots

The shared provider runtime mounts public kubeconfigs, one per migrated root. Each
uses the Pod's rotating ServiceAccount token and Kubernetes impersonation of a named
ServiceAccount in `crossplane-system`. A ProviderConfig selects that root's kubeconfig;
RoleBindings grant that identity only its declared Secret reads and writes. No static
ServiceAccount token Secret is created. Secret creation is namespace-wide because
Kubernetes RBAC cannot restrict `create` by `resourceNames`; updates, reads and deletes
are restricted to the output Secret names.

This scopes normal Kubernetes provider requests, not arbitrary Terraform execution.
All Workspaces still run in the same provider process, whose package-generated role
already grants broad Secret access. Trusted HCL could use that identity directly or
select another mounted kubeconfig. A strict boundary between untrusted roots would
require separate runtimes and controller access scopes; this migration does not supply
that boundary. The PostgreSQL backend credential also remains shared.
