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

- `patches/external-artifact-source.patch`: the `ExternalArtifact` case in `getSource`, its
  field index and watch, the RBAC markers and `config/rbac/role.yaml`, and the `v1alpha2`
  `sourceRef.kind` enum in the generated CRD.
- `patches/api-external-artifact.patch`: the same enum marker and the index key in the nested
  `api` module, a separate archive.
- `patches/bazel.patch`: exports the CRD YAML. Build glue, not an upstream feature.

`external_artifact_source_test.go` covers source resolution and the revision-change mapping;
it lives here, embedding the patched `controllers` library, so it runs without upstream's
envtest suite.

**Gotcha:** the `MODULE.bazel` overrides are build fixes, not features: `//conditions` labels
in `fluxcd/pkg/runtime` resolve to the main repository, and proto generation in `runner/`
would link a second grpc. Both explain themselves in place.

No upstream release supports `ExternalArtifact` sources yet. Remove the module, and switch
`cluster/cdk8s/tofu_controller/release.py` back to the chart's image and CRD, once one does.

## Updating upstream

Bump both proxy archives (the main module's tag and the `api` module's pseudo-version at the
same commit) with their checksums, rebase the patches, regenerate the CRD-carrying
`external-artifact-source.patch` hunk with upstream's `controller-gen` if the schema changed,
and refresh `go.mod`/`go.sum` with `go mod tidy` over the manager's imports (`gomega` and
controller-runtime's `fake` for the test). Keep `cluster/cdk8s/tofu_controller/release.py`'s
chart version on the same release.
