# tofu-controller with ExternalArtifact sources

Builds the pinned upstream [tofu-controller](https://github.com/flux-iac/tofu-controller)
manager with upstream support for a `Terraform` taking a Flux `ExternalArtifact` (as produced
by source-watcher's `ArtifactGenerator`) as its `sourceRef`, so each module can reconcile
from its own artifact instead of the whole repository's (#9271). Go dependencies are isolated
from Ducktape's; `rules_oci` packages the manager into a distroless image.

```bash
bbr build @ducktape_tofu_controller//:image
```

The image publication roster pushes it to `ghcr.io/agentydragon/tofu-controller` (GHCR, not
Forgejo: see `cluster/cdk8s/tofu_controller_release.py`). The cluster runs it through the
upstream Helm chart with the image, CRD and RBAC swapped in by that module.

## Coordinated rollout

The controller and runner must use matching protocols. Update both pins together in
`cluster/k8s/tofu-controller-image-pins/helmrelease-image.yaml`; keep them manual until image
automation can update the pair together.

Before switching the pair, suspend Terraform reconciliations and wait for in-flight work and old
runner pods to finish. A running runner with the current TLS secret can otherwise be reused across
the controller restart. After the new controller is ready, resume reconciliation and verify a
Terraform reconciliation and its runner.

The HelmRelease post-renderer enables `--feature-gates=ExternalArtifact=true`; the pinned chart
does not expose this flag and upstream defaults it to false.

## Source

`go.mod` pins `github.com/flux-iac/tofu-controller` and its nested `api` module to upstream
commit [`e2db61b`](https://github.com/flux-iac/tofu-controller/commit/e2db61bdd264284908d9f7506cb358542a7e3578),
which merged [PR #1901](https://github.com/flux-iac/tofu-controller/pull/1901).
`MODULE.bazel` fetches both official Go proxy zips for that commit with `archive_override`.

`patches/bazel.patch` exports the CRD YAML; `patches/kustomize-embed.patch` makes embedded
Kustomize schemas visible to Bazel. Both are build glue.

The controller's tests, including the `ExternalArtifact` envtest case, live with the upstream
source and run in its own CI: they need a `kube-apiserver` and a `tofu` binary, so Ducktape only
builds the manager.

**Gotcha:** the `MODULE.bazel` overrides are build fixes, not features: `//conditions` labels
in `fluxcd/pkg/runtime` resolve to the main repository, and proto generation in `runner/`
would link a second grpc. Both explain themselves in place.

**TODO:** Once an upstream release includes [PR #1901](https://github.com/flux-iac/tofu-controller/pull/1901),
move these module pins to that release, validate its image and CRD, then remove this module and
switch `cluster/cdk8s/tofu_controller_release.py` back to the chart's image and CRD.

## Updating the controller

Choose an upstream `flux-iac/tofu-controller` commit containing the required behavior. Set both
module requirements in `go.mod` to that commit's pseudo-versions and refresh `go.sum`, then update
both `archive_override`s to the official proxy zips, strip prefixes and SHA-256 values for the same
commit. The chart version is pinned separately in `cluster/cdk8s/tofu_controller_release.py` and
needs its own compatibility review when the controller source changes.
