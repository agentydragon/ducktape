# tofu-controller with ExternalArtifact sources

Builds the pinned upstream [tofu-controller](https://github.com/flux-iac/tofu-controller)
manager with local patches that let a `Terraform` take a Flux `ExternalArtifact` (as produced
by source-watcher's `ArtifactGenerator`) as its `sourceRef`, so each module can reconcile
from its own artifact instead of the whole repository's (#9271). Go dependencies are isolated
from Ducktape's; `rules_oci` packages the manager into a distroless image.

```bash
bbr build @ducktape_tofu_controller//:image
```

The image publication roster pushes it to `ghcr.io/agentydragon/tofu-controller` (GHCR, not
Forgejo: see `cluster/cdk8s/tofu_controller_release.py`). The cluster runs it through the
upstream Helm chart with the image, CRD and RBAC swapped in by that module.

## Source

`go.mod` replaces upstream and its nested `api` module with the `external-artifact-source`
branch of [agentydragon/tofu-controller](https://github.com/agentydragon/tofu-controller):
upstream `main` plus the `ExternalArtifact` change merged upstream (the source kind,
its index, watch and RBAC, regenerated CRD and docs, and an envtest case).
`MODULE.bazel` fetches that commit's Go proxy zips with `archive_override`.

`patches/bazel.patch` exports the CRD YAML: build glue, not part of the branch.

The controller's tests, including the `ExternalArtifact` envtest case, live on the branch
and run in its own CI: they need a `kube-apiserver` and a `tofu` binary, so Ducktape only
builds the manager.

**Gotcha:** the `MODULE.bazel` overrides are build fixes, not features: `//conditions` labels
in `fluxcd/pkg/runtime` resolve to the main repository, and proto generation in `runner/`
would link a second grpc. Both explain themselves in place.

[flux-iac/tofu-controller#1901](https://github.com/flux-iac/tofu-controller/pull/1901) merged upstream,
but no upstream release includes it yet. Once a release includes it, validate the image and CRD,
then remove the module and switch `cluster/cdk8s/tofu_controller_release.py` back to the chart's image and CRD.

## Updating the controller

Push to the fork branch, point both `replace` directives at the new commit and refresh
`go.mod`/`go.sum` with `go mod tidy` over the manager's imports, then set both `archive_override`s to the new
pseudo-versions and their zips' checksums. Keep `cluster/cdk8s/tofu_controller_release.py`'s
chart version on the release the branch is based on.
