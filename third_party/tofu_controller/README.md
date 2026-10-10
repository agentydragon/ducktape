# tofu-controller with ExternalArtifact sources

Builds the pinned upstream [tofu-controller](https://github.com/flux-iac/tofu-controller)
manager with local patches that let a `Terraform` take a Flux `ExternalArtifact` (as produced
by source-watcher's `ArtifactGenerator`) as its `sourceRef`, so each module can reconcile
from its own artifact instead of the whole repository's (#9271). Go dependencies are isolated
from Ducktape's; `rules_oci` packages the manager into a distroless image.

```bash
bbr test @ducktape_tofu_controller//:tests
bbr build @ducktape_tofu_controller//:image
```

The image publication roster pushes it to `ghcr.io/agentydragon/tofu-controller` (GHCR, not
Forgejo: see `cluster/cdk8s/tofu_controller/release.py`). The cluster runs it through the
upstream Helm chart with the image, CRD and RBAC swapped in by that module.

## Patch ownership

`patches/external-artifact-source.patch` and `patches/api-external-artifact.patch` are one
upstream-ready change, split because the nested `api` module is a separate archive:

- the `ExternalArtifact` case in `getSource`, its field index and watch, and the RBAC markers;
- the `v1alpha2` `sourceRef.kind` enum marker and the index key (`api` patch);
- everything upstream regenerates from those (`make manifests api-docs`: CRD, `role.yaml`, the
  chart's CRD copy, the API reference) and the chart's RBAC template;
- `SetupWithManager` taking the manager's context instead of `context.TODO()`;
- an envtest case, `tc000012_src_externalartifact_no_outputs_test.go`, with the source-controller
  `ExternalArtifact` CRD it loads, and a usage page under `docs/use-tf-controller/`.

The upstream change also lets `tfctl create --source` name an `ExternalArtifact`. `tfctl` is a
third Go module this build never fetches, so that hunk is not here.

`patches/bazel.patch` exports the CRD YAML: build glue, not an upstream feature.

`external_artifact_source_test.go` covers source resolution and the revision-change mapping
with a fake client, embedding the patched `controllers` library: upstream's envtest suite needs
a `kube-apiserver` and a `tofu` binary, so it does not run under Bazel here.

**Gotcha:** the `MODULE.bazel` overrides are build fixes, not features: `//conditions` labels
in `fluxcd/pkg/runtime` resolve to the main repository, and proto generation in `runner/`
would link a second grpc. Both explain themselves in place.

No upstream release supports `ExternalArtifact` sources yet. Remove the module, and switch
`cluster/cdk8s/tofu_controller/release.py` back to the chart's image and CRD, once one does.

## Updating upstream

Bump both proxy archives (the main module's tag and the `api` module's pseudo-version at the
same commit) with their checksums. Rebase the change in an upstream checkout, rerun
`make manifests api-docs` there, and re-export both patches from its diff
(`git diff -- . ':!api' ':!tfctl'` and `git diff --relative=api -- api`). Refresh
`go.mod`/`go.sum` with `go mod tidy` over the manager's imports (`gomega` and
controller-runtime's `fake` for the test). Keep `cluster/cdk8s/tofu_controller/release.py`'s
chart version on the same release.
