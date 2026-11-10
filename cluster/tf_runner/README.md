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
- **The runner version** must match the `third_party/tofu_controller` source: the controller
  and runner talk a gRPC protocol that changes between versions. The pinned upstream image in
  `devinfra/bazel/oci.MODULE.bazel` comes from the same commit; update both together.

## Rollout

`devinfra/ci/image_targets.json` publishes it to `ghcr.io/agentydragon/tf-runner` (GHCR, not
Forgejo: the runner provisions Forgejo's registry credentials, so it cannot depend on them).
The HelmRelease's `runner.image` in `cluster/k8s/tofu-controller-image-pins/helmrelease-image.yaml`
is pinned beside the controller image. Update the pair together as described in
<../../third_party/tofu_controller/README.md>.

The GHCR packages must be public (<../docs/container-images.md>) so a fresh cluster can pull
both images before its Forgejo registry credentials exist.
