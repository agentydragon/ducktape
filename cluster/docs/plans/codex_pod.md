# Codex pod → CI-built Nix image, Flux-reconciled (hosted in Forgejo)

Evolving the manual `codex-nix-*-pod` spikes (<../../k8s/agents/x/>, PR #2774) into
a codex pod that tracks the repo: edit the Nix tool env on `devel`, CI builds an
image, Flux rolls the pod — the same build/publish/auto-update pipeline as every
other container, but hosted in **our own Forgejo registry** so there's no manual
GHCR "make public" step.

## Goal

`git push devel` (changing the codex tool env) → CI builds the `codex-pod` Nix
image → pushes to `git.allegedly.works/ducktape-ci/codex-pod` → Flux image
automation writes the new tag into git → the `Deployment` rolls. Adding a tool is a
one-line edit to a Nix `buildEnv`; no `/nix` seeding, no runtime `nix shell`.

## Architecture (implemented on PR #2774)

Three stages, each an existing piece of our stack; pointers are to the real files.

1. **Build** — `dockerTools.buildLayeredImage` over a `buildEnv`, in
   `x/codex_pod_image/default.nix` (flake output `.#codex-pod-image`, following
   `x/nix_rbe_image`). One `buildEnv` = the tool set (`pkgsUnstable.codex` + shell/
   dev tools); adding a tool is one line in `paths`. `tag = null` → content-
   addressed tag (verified). Not nix2container (no new flake input; see
   Alternatives).
2. **Publish** — `.github/workflows/codex-pod-image.yml` builds and `skopeo copy`s
   a `devel-<ts>-<sha7>` tag to Forgejo as the `ducktape-ci` tenant (creds from
   `secrets/ci/forgejo-images-registry.sops.yaml`). Direct push, no proxy.
3. **Auto-migrate** — `cluster/k8s/flux-image-automation-forgejo/` has an
   **authenticated** `ImageRepository` (`secretRef: forgejo-images-creds`) +
   `ImagePolicy`; the cluster-wide `all-images` `ImageUpdateAutomation` rewrites the
   `{"$imagepolicy": "flux-system:codex-pod"}` marker in the Deployment. The pod is
   `cluster/k8s/agents/codex-pod/` (non-root UID 1000, `codex-home` PVC, sshd,
   `imagePullSecrets`), carrying over the SOPS bootstrap identity + sshd from
   `codex-nix-pvc-uid-pod`.

### Why Forgejo, not GHCR

New GHCR packages are private by default and GitHub has **no API** to set
visibility, so each new image needs a one-time manual "make public" click (the
`check public images` guard, PR #2785, only detects it). Hosting in our Forgejo
registry makes the pull credential a code-provisioned SOPS Secret instead. Full
pattern + the availability tradeoff: <../container-images.md> § Forgejo-hosted
images. Credential wiring: the `ducktape-ci` tenant (`tf/gitops/forgejo-images`) +
`forgejo-images-creds` reflected into `flux-system` (scan) and `codex-pod` (pull),
all in `cluster/k8s/forgejo-images/`.

### Roll granularity

CI tags `devel-<ts>-<sha7>` (sortable → `ImagePolicy` picks newest chronologically).
The image's own content-hash tag means unchanged `codexEnv` = identical content; to
avoid rolling on unrelated `devel` commits the workflow is paths-filtered to
`x/codex_pod_image/**` + `flake.lock`. A content-digest push guard (skip if
unchanged) is a possible refinement if churn shows up.

## Alternatives considered (and why not)

| Option                                                          | Why not the mainline                                                                                                                                                                   |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **nix-csi** (Lillecarl) — CSI-mount a shared node `/nix`        | No off-the-shelf auto-roll (store path must reach the spec → custom controller/local render); privileged Talos DaemonSet. Only attractive under a "no CI build" constraint we dropped. |
| **comin** (nlewo) — pull-based NixOS GitOps, in-place `switch`  | The complete "push → self-reconciles" tool, but drives a **NixOS machine/microVM**, not a pod. Tracked for `agent-box` in <../../../idea/comin_nixos_gitops.md>.                       |
| **nix2container** (nlewo) — archive-less, incremental push      | More efficient than `dockerTools` but needs a new flake input + patched skopeo. A future optimization; `dockerTools` matches `x/nix_rbe_image`.                                        |
| **nix-snapshotter** (pdtpartners) — image ref _is_ a store path | Technically ideal, but needs a containerd snapshotter plugin on every node — a Talos-extension project, not a toggle.                                                                  |
| **Nixery** (tazjin)                                             | External/awkward-to-self-host; less control than our own image.                                                                                                                        |
| **kubenix / easykubenix / nixidy**                              | Renderers only; don't change the build/roll story.                                                                                                                                     |
| **kluctl**                                                      | A second GitOps control plane alongside Flux.                                                                                                                                          |

## Remaining — live verification (on merge to `devel`)

Everything is committed but the loop only runs post-merge (tofu-controller, CI,
Flux, reflector are in-cluster). Verify in order:

1. `Terraform/forgejo-images` applies → `ducktape-ci` Forgejo user exists;
   reflector mirrors `forgejo-images-creds` into `flux-system` + `codex-pod`.
   **Watch:** the TF runner reads the secret in the new `forgejo-images` namespace —
   if it fails on RBAC, widen the `tf-runner` role (props reads the `forgejo` ns, so
   cross-ns reads work, but the new ns wasn't verified).
2. `codex-pod-image.yml` pushes the first `git.allegedly.works/ducktape-ci/codex-pod:devel-*`.
3. `flux get image policy codex-pod` resolves the newest tag (authenticated scan
   works) → `all-images` commits the tag into the Deployment marker.
4. Pod pulls with `imagePullSecrets` and reaches `Running`; `kubectl exec` shows
   `codex` on PATH.
5. Loop: edit `x/codex_pod_image/default.nix` → push → pod rolls with the new tool.

## Follow-ups

- **Credentials beyond the SSH identity**: the pod has a minted `codex-pod` key
  (register its pubkey) but no BuildBuddy/Attic/Forgejo-bot creds yet — port from
  `agent-box` (no sops-nix in an image pod, so provide them as secrets/env).
- **Push-time reconcile**: add a Forgejo `package`-webhook receiver (copy
  `cluster/k8s/haku/ui-image-webhook/receiver.yaml`) to replace the 5m poll.
- **Generalize**: once proven, move other `ghcr.io/agentydragon` app images to
  Forgejo image-by-image (weigh the pull-availability tradeoff), and retire Harbor.
- **Exposure/identity**: SSH-over-`kubectl exec` vs a real Service/Cilium listener.

## References

- `x/codex_pod_image/default.nix`, `cluster/k8s/agents/codex-pod/`,
  `cluster/k8s/forgejo-images/`, `cluster/k8s/flux-image-automation-forgejo/`,
  `tf/gitops/forgejo-images/` — the implementation.
- <../container-images.md> — build/push/tag + Forgejo-hosted-images pattern.
- <../../k8s/agents/x/codex-nix-pvc-uid-pod/README.md> — the spike this evolves.
- Alternatives: nix2container <https://github.com/nlewo/nix2container>, nix-csi
  <https://github.com/Lillecarl/nix-csi>, comin <https://github.com/nlewo/comin>,
  nix-snapshotter <https://github.com/pdtpartners/nix-snapshotter>.
