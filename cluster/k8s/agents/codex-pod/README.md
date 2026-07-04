# codex-pod

Codex agent pod running the Nix-built `codex-pod` image
(`x/codex_pod_image/`, `.#codex-pod-image`). Edit the tool set in that `buildEnv`
→ CI builds+pushes the image → Flux image automation rolls this Deployment.
Registry-hosting rationale (why Forgejo over GHCR) + the general pattern:
<../../../docs/container-images.md> § Forgejo-hosted images.

## Pieces

- **Image + env**: `x/codex_pod_image/default.nix` (codex + shell/dev tools).
- **CI**: `.github/workflows/codex-pod-image.yml` builds `.#codex-pod-image` and
  `skopeo copy`s a `devel-<ts>-<sha7>` tag to our Forgejo registry
  `git.allegedly.works/ducktape-ci/codex-pod` (as the `ducktape-ci` tenant).
- **Auto-roll**: `ImageRepository` (authenticated `secretRef`) + `ImagePolicy` in
  `cluster/k8s/flux-image-automation-forgejo/`; the cluster-wide `all-images`
  `ImageUpdateAutomation` writes the resolved tag into `deployment.yaml`'s
  `{"$imagepolicy": "flux-system:codex-pod"}` marker.
- **Registry credential**: `cluster/k8s/forgejo-images/` provisions the
  `ducktape-ci` Forgejo user (Terraform) and a `forgejo-images-creds` Secret
  reflected into `flux-system` (scan) + `codex-pod` (`imagePullSecrets`).
- **Runtime**: non-root UID 1000, `codex-home` PVC (`seaweedfs-ovh`), `sshd` on
  `127.0.0.1:2222` reachable via `kubectl exec` ProxyCommand (see the
  `codex-nix-pvc-uid-pod` spike for the ProxyCommand block).

## Bring-up

Flux-wired (in `cluster/k8s/kustomization.yaml`). Hosting on our own Forgejo
registry means **no manual "make public" step** (the pull credential is
provisioned in code — that's the whole point of the GHCR→Forgejo move). On merge
to `devel`:

1. `forgejo-images` Terraform provisions the `ducktape-ci` Forgejo user;
   reflector mirrors `forgejo-images-creds` into `flux-system` + `codex-pod`.
2. CI builds + pushes the first
   `git.allegedly.works/ducktape-ci/codex-pod:devel-*` image.
3. The `ImagePolicy` resolves the newest tag (authenticated scan), `all-images`
   writes it into the Deployment marker, and the pod pulls with its
   `imagePullSecrets` and runs.
4. Optional: add a Forgejo `package`-webhook receiver (copy
   `cluster/k8s/haku/ui-image-webhook/receiver.yaml`) for push-time pickup
   instead of the 5m `ImageRepository` poll.

## Identity + credentials

`codex-bootstrap-identity.sops.yaml` is a SOPS Secret holding a **freshly minted
`codex-pod` SSH key**; the `plant-identity` init container installs it at
`/home/codex/.ssh/id_ed25519` (0600, 1000:1000). Derive its public key with
`ssh-keygen -y` on the decrypted secret and register it wherever codex must
authenticate.

Unlike `agent-box` (NixOS + home-manager sops-nix), this is an image pod with no
sops-nix, so creds are delivered as k8s Secrets and `start-sshd.sh` renders the
config files at boot (the pod analog of the sops-nix templates):

- **Forgejo git** — the planted `~/.ssh/id_ed25519` pubkey is registered on a
  dedicated `codex-pod` Forgejo user with **read-only** on `agentydragon/ducktape`
  (`tf/gitops/forgejo-agentydragon-repos`). Read-only + **fork model**: codex
  forks the repo and opens PRs from its fork, so it can't push upstream / advance
  `devel` (all agent users follow this — see that module's header). `start-sshd.sh`
  writes the `git.allegedly.works` ssh matchBlock (push to the fork uses the same
  key/host); `FORGEJO_{USERNAME,PASSWORD,URL}` (from `codex-pod-forgejo-creds`,
  also written by that TF) give the agent the API creds SSH can't provide, to
  create the fork + open the PR.
- **BuildBuddy** — `BUILDBUDDY_API_KEY` is set on the container from the shared
  `buildbuddy-api-key` Secret via `secretKeyRef` (`optional: true`); `bbr` reads
  it, and `start-sshd.sh` forwards it to ssh sessions via `SetEnv`. That Secret
  (`cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml`) is reflected
  into `codex-pod` — no per-pod key, just the one shared key.

## Follow-ups

- **Attic** — `~/.config/attic/config.toml` + `~/.config/nix/netrc` from a minted
  token, rendered at boot like the others (not wired yet).
- **Push-time reconcile** — a Forgejo `package`-webhook receiver (copy
  `cluster/k8s/haku/ui-image-webhook/receiver.yaml`) to replace the 5m
  `ImageRepository` poll.
- **Generalize** — once proven, move other `ghcr.io/agentydragon` app images to
  Forgejo image-by-image (weigh the per-image pull-availability tradeoff — an
  in-cluster registry outage means those pods can't pull), and retire Harbor.
- **Exposure** — SSH-over-`kubectl exec` (current) vs a real Service / Cilium
  listener.
- **Bring-up watch** — the `forgejo-images` Terraform reads the credential in the
  new `forgejo-images` namespace; if the first apply errors on RBAC, widen the
  `tf-runner` role (`forgejo-props` reads the `forgejo` ns, so cross-ns reads
  work, but the new ns wasn't verified).
