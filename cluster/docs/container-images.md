# Container Images

**New in-cluster images default to our own Forgejo registry** (`git.allegedly.works`,
operator decision on #5003): a private package pulled with a bot credential provisioned
in code, no manual visibility step — see [Forgejo-hosted images](#forgejo-hosted-images)
below. Weigh the tradeoff there before using it for a pull-critical workload.

GHCR (`ghcr.io/agentydragon/<image>`) is the exception, for images that must be
publicly pullable or must survive a Forgejo outage. Most existing images still live
there. GHCR packages must be public (no pull credentials on cluster nodes) — and
because GitHub exposes **no API to set package visibility**, a new GHCR package must be
flipped public **once, by hand, in the UI** (the `check public images` CI guard
detects any that aren't).

Props agent runtime images are published to the Forgejo registry via the props
registry proxy (`props.allegedly.works` → `git.allegedly.works/props/*`).

## Adding a new container image

1. **Build**: Prefer Bazel (`oci_image` + `oci_load` from `@rules_oci`). Use GitHub
   Actions only when Bazel can't build it (e.g., multi-stage Docker builds with
   external toolchains). Set `org.opencontainers.image.source` label.
   Example: <../../third_party/activitywatch/BUILD.bazel>

2. **Push**: Add an entry to <../../devinfra/ci/image_targets.json> (the image roster
   SSOT the `push-images` workflow reads): the key is the published image name, with
   `target` (the Bazel `oci_image` label), `test` (its test gate), and — the default
   for new in-cluster images — `"registry": "forgejo"`; omitting `registry` publishes
   to GHCR. The workflow builds the image on RBE, downloads it to the runner, and
   `crane push`es it. Pushes are deduped by content digest against the newest existing
   `devel-*` tag — unchanged images are no-ops, so Flux ImagePolicy doesn't churn
   deployments. Set `org.opencontainers.image.source` on the `oci_image` to link a
   GHCR package to the repo on first push.

3. **Tag policy — avoid `:latest`**: Use Flux image automation to track pinned tags
   (`{branch}-{timestamp}-{sha7}`). For in-cluster images:
   - Create `ImageRepository` + `ImagePolicy` in `k8s/flux-image-automation-forgejo/`
     (with `secretRef: forgejo-images-creds` on the repository) or, for a GHCR image,
     in `k8s/flux-image-automation-ghcr/`
   - Add `{"$imagepolicy": "flux-system:<policy-name>"}` comment to the image field
   - **GHCR only: add the `ImageRepository` to the GitHub webhook receiver** at
     `k8s/flux-webhook/github-webhook-receiver.yaml` — without this, the image only
     gets picked up on the 5m poll interval instead of immediately on push. Forgejo
     images ride the poll; the webhook is GitHub's.
   - Flux updates the tag in-repo on each new push

   For images not deployed in-cluster, pin the tag in the consuming BUILD.bazel.

4. **GitHub Actions path** (when Bazel isn't viable):
   - Workflow in `.github/workflows/<name>-image.yml`
   - Use `docker/build-push-action` with `packages: write` permission

**Gotcha — Flux image automation race**: When renaming image paths, push at least one
image to the new path before updating `ImageRepository` resources. Otherwise Flux reverts
to the old path.

## Forgejo-hosted images

The default for a new in-cluster image: a **private** package in our own registry (no
GHCR "make public" toil, credential provisioned in code). Reference implementation:
`codex-pod`.

1. **Tenant + credential** — `cluster/k8s/forgejo-images/` provisions the
   `ducktape-ci` Forgejo user (`tf/gitops/forgejo-images`, password from the SOPS
   `forgejo-images-creds` Secret) and reflects that Secret (dockerconfigjson) into
   `flux-system` (Flux scan `secretRef`) and consuming namespaces (kubelet
   `imagePullSecrets`). CI reads the same creds from
   `secrets/ci/forgejo-images-registry.sops.yaml` (via `setup-ci-secrets`).
2. **Push** — for Bazel-built images, set `"registry": "forgejo"` on the image's
   entry in <../../devinfra/ci/image_targets.json>; the workflow pushes
   `git.allegedly.works/ducktape-ci/<image>:<tag>` with the same
   `{branch}-{timestamp}-{sha7}` tag. For bespoke workflows, use
   `skopeo copy … docker://git.allegedly.works/ducktape-ci/<image>:<tag>
--dest-creds "$FORGEJO_IMAGES_USERNAME:$FORGEJO_IMAGES_PASSWORD"`. Direct to
   Forgejo — no proxy (unlike props).
3. **Auto-roll** — add `ImageRepository` (with `secretRef: forgejo-images-creds`)
   - `ImagePolicy` under `cluster/k8s/flux-image-automation-forgejo/`, and the
     `{"$imagepolicy": "flux-system:<name>"}` marker on the image field. The
     cluster-wide `all-images` `ImageUpdateAutomation` updates it.
4. **Consume** — image `git.allegedly.works/ducktape-ci/<image>` +
   `imagePullSecrets: [{name: forgejo-images-creds}]`; the app's Flux Kustomization
   `dependsOn: forgejo-images`.

**Tradeoff**: unlike GHCR (external), a Forgejo outage means these pods can't pull.
Fine for non-critical workloads (agent pods); weigh per-image before moving
pull-critical ones.
