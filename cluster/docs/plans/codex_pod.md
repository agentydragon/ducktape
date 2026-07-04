# Codex pod → CI-built Nix image, Flux-reconciled

Design for evolving the manual `codex-nix-*-pod` spikes (<../../k8s/agents/x/>,
PR #2774) into a codex pod that tracks the repo: edit the Nix tool environment on
`devel`, CI builds an image from it, and Flux rolls the pod automatically — the
same build/publish/auto-update pipeline we already use for every other container
(<../container-images.md>).

## Goal

`git push devel` (changing `flake.nix` / the codex tool env) → CI builds a
`nix2container` image → pushes to the registry → Flux image automation writes the
new digest to git → the codex `Deployment` rolls. Adding a tool is a one-line edit
to a Nix `buildEnv`; no hand-rolled `/nix` seeding, no runtime `nix shell`.

## Current state (the spikes)

Three manually-applied, non-Flux pods under `agents/x/` (PR #2774):

- `codex-nix-pod` — writable `/nix` on a PVC (root).
- `codex-nix-image-pod` — image-backed `/nix`, no PVC (root).
- `codex-nix-pvc-uid-pod` — non-root UID 1000, disk-backed `emptyDir` `/nix`
  seeded in an init container, SOPS bootstrap identity, sshd-over-`kubectl exec`.

All three hand-roll "get a `/nix` into the pod" then `nix shell nixpkgs#…` at
runtime (~77s cold start, 80Gi `emptyDir` per pod). The image approach below
replaces that: the tool set is baked into an image, so there's nothing to seed.

## Chosen architecture: CI-built Nix image + Flux image automation

Three stages, each an existing, boring piece of our stack:

1. **Build** — CI builds the image from the flake on every `devel` commit
   (analogous to `openclaw-image` / the `push-images` matrix). We accepted a CI
   build step, so there is no in-cluster builder, no `nix-csi`, no custom
   controller.
2. **Publish** — push to the registry (GHCR / Harbor), same as other images.
3. **Auto-migrate** — Flux `ImageRepository` + `ImagePolicy` +
   `ImageUpdateAutomation` scan the tag, commit the new digest to git, Flux
   applies, the `Deployment` rolls. Identical to how every other image
   auto-updates here.

### The environment (SSOT for the tool set)

```nix
# codex-env.nix
{ pkgs }:
pkgs.buildEnv {
  name = "codex-env";
  paths = with pkgs; [
    bash coreutils moreutils git openssh
    direnv nix-direnv
    ripgrep fd jq nodejs_22
    # codex-cli: nixpkgs pkg or our own overlay derivation
  ];
}
```

Adding a tool = one line here. This `buildEnv` is the only thing that defines
what lands in the pod.

### The image (nix2container)

```nix
# image.nix
nix2container.buildImage {
  name = "ghcr.io/agentydragon/codex-pod";
  # tag defaults to the image output hash → changes iff the closure changes
  config.entrypoint = [ "${pkgs.bash}/bin/bash" ];
  copyToRoot = codexEnv; # buildEnv above, relocated to /
  maxLayers = 100; # popularity-based layering for cache reuse
}
```

`nix build .#codexImage` produces a small JSON descriptor (no tarball in the
store); `nix run .#codexImage.copyToRegistry` streams only changed layers to the
registry. See <../container-images.md> for our registry/tag/Flux-automation
conventions.

### The CI job (sketch — analogous to `openclaw-image`)

A dedicated workflow that runs Nix (we already run Nix in CI for the Attic push):

```yaml
# .github/workflows/codex-pod-image.yml (sketch)
on:
  push:
    branches: [devel]
    paths: ["cluster/k8s/agents/codex-pod/**", "flake.nix", "flake.lock"]
jobs:
  build-push:
    steps:
      - uses: actions/checkout@v4
      - # install/enable Nix
      - run: nix run .#codexImage.copyToRegistry # → GHCR/Harbor
```

### Flux image automation (sketch)

```yaml
# ImageRepository scans the registry; ImagePolicy selects the newest tag;
# ImageUpdateAutomation writes image:tag@sha256:<digest> back into the Deployment.
apiVersion: image.toolkit.fluxcd.io/v1beta2
kind: ImageRepository
metadata: { name: codex-pod, namespace: flux-system }
spec:
  image: ghcr.io/agentydragon/codex-pod
  interval: 5m
---
apiVersion: image.toolkit.fluxcd.io/v1beta2
kind: ImagePolicy
metadata: { name: codex-pod, namespace: flux-system }
spec:
  imageRepositoryRef: { name: codex-pod }
  policy: { numerical: { order: asc } } # exact policy per container-images.md
```

### The Deployment (sketch)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: codex-pod, namespace: codex-pod }
spec:
  replicas: 1
  template:
    spec:
      securityContext: { runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000 }
      containers:
        - name: codex
          image: ghcr.io/agentydragon/codex-pod:latest # {"$imagepolicy": "flux-system:codex-pod"}
          command: ["bash", "-lc", "…start sshd…"]
          volumeMounts:
            - { name: home, mountPath: /home/codex }
      volumes:
        - name: home
          persistentVolumeClaim: { claimName: codex-home } # seaweedfs-ovh
```

The `# {"$imagepolicy": …}` marker is where `ImageUpdateAutomation` writes the
resolved `tag@digest`. Non-root UID 1000, the SOPS bootstrap identity, and
home-on-SeaweedFS carry over unchanged from `codex-nix-pvc-uid-pod`.

### Roll granularity

nix2container output is deterministic, so an unchanged `codexEnv` yields the same
content. To avoid rolling the pod on unrelated `devel` commits, the CI job should
only publish a new **sortable** tag when the closure actually changed (e.g. skip
the push if a tag for this narhash already exists), so `ImagePolicy` only sees a
new image when the tool set really moved. Exact tag policy: settle against
<../container-images.md>.

## Alternatives considered (and why not, for now)

| Option | Why not the mainline |
| --- | --- |
| **nix-csi** (Lillecarl) — CSI-mount a shared node `/nix`, realize closure in-cluster | No off-the-shelf auto-roll: the store path must reach the `Deployment` spec, needing a custom controller or local render. Also a privileged Talos DaemonSet to validate. Was only attractive under a "no CI build" constraint we've since dropped. |
| **comin** (nlewo) — pull-based NixOS GitOps, in-place `nixos-rebuild switch` | The genuinely complete "push → self-reconciles" tool, but it drives a **NixOS machine/microVM**, not a k8s pod. Tracked separately for the `agent-box` VM in <../../../idea/comin_nixos_gitops.md>. |
| **nix-snapshotter** (pdtpartners) — image ref *is* a nix store path, closure from a binary cache | Technically ideal (no fat layers), and Flux image automation can still watch the registry tag. Blocked by needing a **containerd snapshotter plugin on every node** — a Talos-extension/custom-image project, not a config toggle. |
| **Nixery** (tazjin) — `nixery.dev/shell/pkg1/pkg2` as the image | Quick ergonomics, but an external/awkward-to-self-host registry and less control than building our own image. |
| **kubenix / easykubenix / nixidy** — Nix → k8s manifests | Renderers only; don't change the build/roll story. Overkill for one Deployment. |
| **kluctl** — templated deploy tool | A second GitOps control plane alongside Flux. Rejected. |

## Open questions / decisions

- **Registry**: GHCR (like `openclaw-image`) vs in-cluster Harbor.
- **`codex-cli` packaging**: is it in nixpkgs, or do we need an overlay/derivation?
- **Manifest location**: promote to `cluster/k8s/agents/codex-pod/` with a Flux
  `Kustomization` + SOPS decryption once the image builds and the pod runs.
- **Identity**: reuse `agent-box-codex-user` or mint a `codex-pod` identity.
- **Exposure**: SSH-over-`kubectl exec` (as in the spike) vs a real Service /
  Cilium listener.
- **Tag policy / roll granularity**: sortable tag + content guard (above).

## Phased next steps

1. Land the spikes (PR #2774) — done / in review.
2. Add `codex-env.nix` + `image.nix`; get `nix run .#codexImage.copyToRegistry`
   working locally against the registry.
3. Add the CI workflow (paths-filtered on the codex env + flake).
4. Add the `Deployment` + Flux `ImageRepository`/`ImagePolicy`/
   `ImageUpdateAutomation` under `cluster/k8s/agents/codex-pod/`; wire the Flux
   `Kustomization` (SOPS for the identity secret).
5. Verify the loop end-to-end: edit `codex-env.nix` → push → new image → digest
   committed → pod rolls with the new tool present.
6. Decide exposure + identity, then treat as a normal service.

## References

- <../../k8s/agents/x/codex-nix-pvc-uid-pod/README.md> — the spike this evolves.
- <../container-images.md> — our build/push/tag + Flux image automation conventions.
- nix2container: <https://github.com/nlewo/nix2container>.
- Alternatives: nix-csi <https://github.com/Lillecarl/nix-csi>, comin
  <https://github.com/nlewo/comin> (see <../../../idea/comin_nixos_gitops.md>),
  nix-snapshotter <https://github.com/pdtpartners/nix-snapshotter>.
