# codex-pod

Codex agent pod running the Nix-built `codex-pod` image
(`x/codex_pod_image/`, `.#codex-pod-image`). Edit the tool set in that `buildEnv`
→ CI builds+pushes the image → Flux image automation rolls this Deployment. Full
design: <../../../docs/plans/codex_pod.md>.

## Pieces

- **Image + env**: `x/codex_pod_image/default.nix` (codex + shell/dev tools).
- **CI**: `.github/workflows/codex-pod-image.yml` builds `.#codex-pod-image` and
  `skopeo copy`s a `devel-<ts>-<sha7>` tag to `ghcr.io/agentydragon/codex-pod`.
- **Auto-roll**: `ImageRepository` + `ImagePolicy` in
  `cluster/k8s/flux-image-automation-ghcr/`; the cluster-wide `all-images`
  `ImageUpdateAutomation` writes the resolved tag into `deployment.yaml`'s
  `{"$imagepolicy": "flux-system:codex-pod"}` marker.
- **Runtime**: non-root UID 1000, `codex-home` PVC (`seaweedfs-ovh`), `sshd` on
  `127.0.0.1:2222` reachable via `kubectl exec` ProxyCommand (see the
  `codex-nix-pvc-uid-pod` spike for the ProxyCommand block).

## Activation (not yet Flux-wired)

This directory is **not** in `cluster/k8s/kustomization.yaml` yet — bring it up
after the image exists:

1. Merge to `devel` so the CI workflow builds + pushes the first image.
2. **Make the GHCR package public** (`ghcr.io/agentydragon/codex-pod`) — cluster
   nodes pull without credentials (see <../../../docs/container-images.md>).
3. Add `cluster/k8s/agents/codex-pod/flux-kustomization.yaml` to the root
   `cluster/k8s/kustomization.yaml`. Flux applies the Deployment; image
   automation pins it to the newest `devel-*` tag.
4. Optional: add the `codex-pod` `ImageRepository` to
   `cluster/k8s/flux-webhook/github-webhook-receiver.yaml` for push-time pickup
   instead of the 5m poll.

## Follow-ups (credentials)

The first cut runs `sshd` + `codex` but has **no codex identity/credentials** —
it can't push to Forgejo, use BuildBuddy, or push to Attic yet. To make it useful
for real work, port from the `agent-box` codex user
(<../../../../nix/home/hosts/agent-box.nix>):

- a SOPS bootstrap identity Secret for `/home/codex/.ssh/id_ed25519` (mint a fresh
  `codex-pod` key or reuse `agent-box-codex-user`; register the pubkey in Forgejo)
  - an init container that plants it 0600/1000:1000 (as in the spike);
- BuildBuddy API key, Attic token, Forgejo bot key, kubeconfig — the same
  home-manager modules `agent-box.nix` imports, or their in-cluster equivalents.
