# Container images

Use the private Forgejo registry for new in-cluster images. A Forgejo outage can prevent a pod on a new node
from pulling its image. Use GHCR when the workload must survive that outage or the image needs anonymous
distribution. [Registry decision](https://github.com/agentydragon/ducktape/pull/5003#issuecomment-5447713281)

## Adding a new container image

**Publish the image.** Choose the build path that owns it.

- **Bazel.** Declare the `oci_image` and `oci_load`, set the `org.opencontainers.image.source` label, and add
  the published name to the image roster. Set `target` to the OCI image label, `test` to the applicable test
  gate, and `registry` to `forgejo`. Omitting `registry` selects GHCR. On devel the planner and push job
  compare the image digest with the registry's `latest` tag. A changed digest gets a pinned
  `devel-<timestamp>-<sha7>` tag. [Build example](../../third_party/activitywatch/BUILD.bazel) ·
  [Image roster](../../devinfra/ci/image_targets.json) ·
  [Publication decisions](../../devinfra/ci/docs/publish_planning.md)
- **Nix.** For a flake image or KubeVirt containerDisk, add the flake attribute, repository, input paths, and
  optional smoke script to the Nix roster. Include new input paths in the workflow triggers so a change starts
  CI. PRs build selected images and run their verification gates. Devel publishes verified images.
  [Nix image roster](../../devinfra/ci/nix_images.json) ·
  [Nix image workflow](../../.github/workflows/nix-images.yml)
- **Bespoke workflows.** Use a dedicated GitHub Actions workflow when neither build path fits. Authenticate to
  the destination registry before pushing. For GHCR, grant `packages: write`. Forgejo uses the Actions secrets
  described below. The Props agent publisher has a separate registry proxy and digest/tag scheme. Its workflow
  job is parked and records the prerequisites for revival.
  [Props publisher job](../../.github/workflows/push-images.yml)

**Track the pinned tag in Flux** when the cluster deploys the image.

1. For a `ducktape-ci` Forgejo image, add its policy name to `IMAGES`. The roster generates its
   `ImageRepository`, newest-tag `ImagePolicy`, and package webhook Receiver entry. Use the existing
   package-name override when the policy and registry names differ. For GHCR, define the repository and policy
   in the owning chart and add the repository to the GitHub Receiver. The `tf-runner` chart shows that path.
   [Forgejo roster](../cdk8s/forgejo_registry/image_automation.py) ·
   [GHCR chart example](../cdk8s/tofu_controller_release.py) · [GitHub Receiver](../cdk8s/flux_webhook.py)
2. Pin the image in the workload's hand-written `image-pins/kustomization.yaml` under `cluster/k8s/`. A
   `newTag` field takes a **tag-only** marker such as `# {"$imagepolicy": "flux-system:airlock:tag"}`. The
   bare marker writes the full image reference into that field and breaks the pull. Regenerate the manifests
   with `bb run //cluster/cdk8s:generate_manifests`.
   [Airlock example](../k8s/agents/airlock/image-pins/kustomization.yaml) ·
   [cdk8s image pin conventions](cdk8s.md#live-image-automation)
3. When renaming a registry path, publish the first tagged image at the new path before switching Flux to it.
   Otherwise automation can revert the workload to the old path. For an image consumed outside the cluster,
   pin the published tag in the consuming build or configuration.

For a new GHCR package, set its visibility to public in the GitHub package settings after the first push and
verify an anonymous pull of the pinned tag.
[GitHub package visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

## Forgejo-hosted images

The tenant is `git.allegedly.works/ducktape-ci`. OpenTofu provisions the user and package webhook from the
cluster-owned credential. The GitHub secrets sync supplies `FORGEJO_IMAGES_USERNAME` and
`FORGEJO_IMAGES_PASSWORD` to publishing workflows. [Tenant module](../../tf/gitops/forgejo-images) ·
[GitHub secrets sync](../../tf/gitops/github-secrets-sync/README.md)

The registry chart creates a copy of `forgejo-images-creds` in `flux-system` for authenticated image scans. In
a workload namespace, use the pull-credentials helper to create the Docker config Secret through ESO and
reference it in `imagePullSecrets`. Kubelet retries the pull while the credential arrives.
[Registry chart](../cdk8s/forgejo_registry/chart.py) ·
[Pull-credentials helper](../cdk8s/forgejo_registry/pull_credentials.py)

The archived Codex pod documents an earlier Forgejo consumer and its retired publication and image scan.
[Codex pod deployment](../../x/codex_pod_image/deploy/README.md)
