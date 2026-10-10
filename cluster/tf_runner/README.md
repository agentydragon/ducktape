# tf-runner image

The image tofu-controller starts a runner pod from for every `Terraform` CR: upstream's
`ghcr.io/flux-iac/tf-runner` plus every Terraform provider the repository pins, so `tofu init`
in the cluster downloads nothing.

- **Providers** come from the same `tf.download(mirror = ...)` set in `MODULE.bazel` that
  `tf_module` validate and lint use, unpacked under `/usr/share/tofu/providers`. From an
  unpacked mirror `tofu init` symlinks each provider into `.terraform/` rather than unzipping
  it, so a run writes no provider bytes at all.
- **`TF_CLI_CONFIG_FILE`** points at a `tofurc` whose only installation method is that
  directory. A module needing a provider or version outside the mirror fails `tofu init`;
  fix it by changing the module or the mirror, never by adding a `direct` fallback.
- **The base tag** must match the release `third_party/tofu_controller`'s fork branch is
  based on: the controller and runner talk a gRPC protocol that changes between releases. The
  fork leaves `runner/` untouched, so upstream's runner of that release is the match. Bump both
  together.

## Rollout

`devinfra/ci/image_targets.json` publishes it to `ghcr.io/agentydragon/tf-runner` (GHCR, not
Forgejo: the runner provisions Forgejo's registry credentials, so it cannot depend on them).
The `tf-runner` ImagePolicy (`cluster/cdk8s/tofu_controller/release.py`) picks the newest
build, and Flux writes it into the HelmRelease's `runner.image` in
`cluster/k8s/tofu-controller-image-pins/helmrelease-image.yaml`, beside the controller's pin.

**Deviation:** the pins start at upstream's runner and the markers rewrite the repository as
well as the tag, so nothing changes until Flux can scan our image. A new GHCR package is
private, so that first switch waits for the package to be made public by hand
(<../docs/container-images.md>).
