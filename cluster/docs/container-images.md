# Container Images

Most ducktape project images are published to GHCR at `ghcr.io/agentydragon/<image>`.
GHCR packages must be public (no pull credentials on cluster nodes) — and because
GitHub exposes **no API to set package visibility**, a new GHCR package must be
flipped public **once, by hand, in the UI** (the `check public images` CI guard
detects any that aren't). To avoid that toil, new in-cluster images can instead be
hosted in **our own Forgejo registry** (`git.allegedly.works`) as a private
package pulled with a bot credential provisioned in code — see
[Forgejo-hosted images](#forgejo-hosted-images) below.

Props agent runtime images are published to the Forgejo registry via the props
registry proxy (`props.allegedly.works` → `git.allegedly.works/props/*`).

## Adding a new container image

1. **Build**: Prefer Bazel (`oci_image` + `oci_load` from `@rules_oci`). Use GitHub
   Actions only when Bazel can't build it (e.g., multi-stage Docker builds with
   external toolchains). Set `org.opencontainers.image.source` label.
   Example: <../k8s/activitywatch/BUILD.bazel>

2. **Push**: Add a row to the matrix in `.github/workflows/push-images.yml`:
   `{ image: "//pkg:image", image_name: "<ghcr-name>", test_target: "//pkg/..." }`.
   The workflow builds the image on RBE, downloads it to the runner, and `crane push`es
   to `ghcr.io/agentydragon/<image_name>`. Pushes are deduped by content digest against
   the newest existing `devel-*` tag — unchanged images are no-ops, so Flux ImagePolicy
   doesn't churn deployments. Set `org.opencontainers.image.source` on the `oci_image`
   to link the package to the repo on first push.

3. **Tag policy — avoid `:latest`**: Use Flux image automation to track pinned tags
   (`{branch}-{timestamp}-{sha7}`). For in-cluster images:
   - Create `ImageRepository` + `ImagePolicy` in `k8s/flux-image-automation-ghcr/`
   - Add `{"$imagepolicy": "flux-system:<policy-name>"}` comment to the image field
   - **Add the `ImageRepository` to the GitHub webhook receiver** at
     `k8s/flux-webhook/github-webhook-receiver.yaml` — without this, the image
     only gets picked up on the 5m poll interval instead of immediately on push
   - Flux updates the tag in-repo on each new push

   For images not deployed in-cluster, pin the tag in the consuming BUILD.bazel.

4. **GitHub Actions path** (when Bazel isn't viable):
   - Workflow in `.github/workflows/<name>-image.yml`
   - Use `docker/build-push-action` with `packages: write` permission

**Gotcha — Flux image automation race**: When renaming image paths, push at least one
image to the new path before updating `ImageRepository` resources. Otherwise Flux reverts
to the old path.

## Forgejo-hosted images

For a **private** image in our own registry (no GHCR "make public" toil, credential
provisioned in code), host it in Forgejo. Reference implementation: `codex-pod`.

1. **Tenant + credential** — `cluster/k8s/forgejo-images/` provisions the
   `ducktape-ci` Forgejo user (`tf/gitops/forgejo-images`, password from the SOPS
   `forgejo-images-creds` Secret) and reflects that Secret (dockerconfigjson) into
   `flux-system` (Flux scan `secretRef`) and consuming namespaces (kubelet
   `imagePullSecrets`). CI reads the same creds from
   `secrets/ci/forgejo-images-registry.sops.yaml` (via `setup-ci-secrets`).
2. **Push** — `skopeo copy … docker://git.allegedly.works/ducktape-ci/<image>:<tag>
--dest-creds "$FORGEJO_IMAGES_USERNAME:$FORGEJO_IMAGES_PASSWORD"` (same
   `{branch}-{timestamp}-{sha7}` tag). Direct to Forgejo — no proxy (unlike props).
3. **Auto-roll** — add `ImageRepository` (with `secretRef: forgejo-images-creds`)
   - `ImagePolicy` under `cluster/k8s/flux-image-automation-forgejo/`, and the
     `{"$imagepolicy": "flux-system:<name>"}` marker on the image field. The
     cluster-wide `all-images` `ImageUpdateAutomation` updates it.
4. **Consume** — image `git.allegedly.works/ducktape-ci/<image>` +
   `imagePullSecrets: [{name: forgejo-images-creds}]`; the app's Flux Kustomization
   `dependsOn: forgejo-images`.

**Tradeoff**: unlike GHCR (external) or the Talos→Harbor pull-through mirror (which
falls back upstream), a Forgejo outage means these pods can't pull. Fine for
non-critical workloads (agent pods); weigh per-image before moving pull-critical ones.
