# codex-pod

Codex agent pod running the Nix-built `codex-pod` image
(`x/codex_pod_image/`, `.#codex-pod-image`). Edit the tool set in that `buildEnv`
→ CI builds+pushes the image → Flux image automation rolls this Deployment.
Registry-hosting rationale (why Forgejo over GHCR) + the general pattern:
<../../../docs/container-images.md> § Forgejo-hosted images.

## Pieces

- **Image + env**: `x/codex_pod_image/default.nix` — a `buildEnv` tool set on
  `/bin` plus the user's static dotfiles baked from a home-manager config
  (`x/codex_pod_image/home.nix`). No runtime bootstrap script; all static config
  is Nix-built and baked into the image.
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
- **Runtime**: non-root UID 1000. `HOME=/home/codex` (the baked dotfiles) is
  distinct from the work dir: the `codex-workspace` PVC (`seaweedfs-ovh`) mounts at
  `/workspace` so it doesn't shadow the baked home, and holds the work tree,
  `CODEX_HOME` (`/workspace/.codex`), and `XDG_CACHE_HOME` (`/workspace/.cache`).
  Access is `kubectl exec` (no sshd) — the container's baked env carries into exec
  sessions.

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

Config is baked (home-manager, in the image); only **secrets** arrive at runtime,
from k8s — the image carries no sops-nix (that needs a systemd user manager, which
this non-root image pod has not) and no boot-time render script:

- **SSH key** — `codex-bootstrap-identity.sops.yaml` is a SOPS Secret holding a
  **freshly minted `codex-pod` SSH key**, mounted read-only at `/run/codex-secret`.
  The container's one runtime step plants it:
  `install -D -m600 /run/codex-secret/id_ed25519 "$HOME/.ssh/id_ed25519"` (ssh
  demands 0600 owned by the user, which a mount can't satisfy). Derive its public
  key with `ssh-keygen -y` on the decrypted secret.
- **Forgejo git** — the planted key's pubkey is registered on a dedicated
  `codex-pod` Forgejo user with **read-only** on `agentydragon/ducktape`
  (`tf/gitops/forgejo-agentydragon-repos`). Read-only + **AGit**: codex proposes
  changes with `git push origin HEAD:refs/for/devel -o topic=<t>`, which opens/
  updates a PR using only read access — no write, no fork, no API token; the SSH
  key is the only credential (all agent users follow this — see that module's
  header). The `git.allegedly.works` ssh matchBlock is baked by home-manager
  (`home.nix`), not rendered at boot.
- **BuildBuddy** — `BUILDBUDDY_API_KEY` is set on the container from the shared
  `buildbuddy-api-key` Secret via `secretKeyRef` (`optional: true`); `bbr` reads
  it. That Secret
  (`cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml`) is reflected
  into `codex-pod` — no per-pod key, just the one shared key.

## Follow-ups

- **Attic** — `~/.config/attic/config.toml` + `~/.config/nix/netrc` from a minted
  token. The static parts belong in `home.nix`; the token would come from k8s via
  an ESO `ExternalSecret` `spec.target.template` that renders the netrc (precedent:
  `cluster/k8s/props/app/registry-pull-secret.yaml`) — not wired yet.
- **Push-time reconcile** — a Forgejo `package`-webhook receiver (copy
  `cluster/k8s/haku/ui-image-webhook/receiver.yaml`) to replace the 5m
  `ImageRepository` poll.
- **Generalize** — once proven, move other `ghcr.io/agentydragon` app images to
  Forgejo image-by-image (weigh the per-image pull-availability tradeoff — an
  in-cluster registry outage means those pods can't pull), and retire Harbor.
- **Bring-up watch** — the `forgejo-images` Terraform reads the credential in the
  new `forgejo-images` namespace; if the first apply errors on RBAC, widen the
  `tf-runner` role (`forgejo-props` reads the `forgejo` ns, so cross-ns reads
  work, but the new ns wasn't verified).
