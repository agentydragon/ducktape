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

## Bring-up

Flux-wired (in `cluster/k8s/kustomization.yaml`). On merge to `devel`:

1. CI builds + pushes the first `ghcr.io/agentydragon/codex-pod:devel-*` image.
2. **Make the GHCR package public** — one-time manual step; cluster nodes pull
   without credentials (see <../../../docs/container-images.md>). Until then the
   `ImageRepository` scan fails and the pod stays `ImagePullBackOff` on the
   placeholder `:devel` tag.
3. Once public, the `ImagePolicy` resolves the newest tag, the `all-images`
   `ImageUpdateAutomation` writes it into the Deployment marker, and the pod runs.
4. Optional: add the `codex-pod` `ImageRepository` to
   `cluster/k8s/flux-webhook/github-webhook-receiver.yaml` for push-time pickup
   instead of the 5m poll.

## Identity + credentials

`codex-bootstrap-identity.sops.yaml` is a SOPS Secret holding a **freshly minted
`codex-pod` SSH key**; the `plant-identity` init container installs it at
`/home/codex/.ssh/id_ed25519` (0600, 1000:1000). Derive its public key with
`ssh-keygen -y` on the decrypted secret and register it wherever codex must
authenticate.

Still **no other credentials** — the pod can't use BuildBuddy or push to Attic
yet. Unlike `agent-box` (NixOS + home-manager sops-nix chaining off this key),
this is an image pod with no sops-nix activation, so BuildBuddy/Attic/Forgejo-bot
creds must be provided as their own secrets/env rather than derived from the
planted key. Port from the `agent-box` codex user
(<../../../../nix/home/hosts/agent-box.nix>) as a follow-up.
