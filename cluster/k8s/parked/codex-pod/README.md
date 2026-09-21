# codex-pod

**Retired, unwired from Flux** (moved to `cluster/k8s/parked/`; it has no entry in
the central Flux Kustomization chart). Its Deployment had no OVH nodeSelector, so
the scheduler could place it on non-OVH nodes (e.g. `wyrm2`) where the SeaweedFS CSI
driver isn't present, and its `codex-workspace` PVC then failed to attach — the
Deployment sat `ProgressDeadlineExceeded` rather than being fixed. `agent-sandbox`'s
`SandboxTemplate`-based codex workspace (`cluster/k8s/agents/agent-sandbox/`) is the
newer pattern for this workload. Manifests kept here for reference; re-wiring means
restoring the image build/publish workflow and Flux image scan/policy, adding its Flux
Kustomization to the central cdk8s chart, and fixing the nodeSelector gap.

The Nix image source (`x/codex_pod_image/`) and `.#codex-pod-image` output remain
in the repo, but its current flake evaluation fails because the image imports
`codex-claude.nix` without its required `config` argument. Automatic build/publish and
Flux image scanning are retired; the evaluation issue is a separate, deferred change.
Registry-hosting rationale (why Forgejo over GHCR) + the general pattern:
<../../../../docs/container-images.md> § Forgejo-hosted images.

## Historical image build workflow

While active, `.github/workflows/codex-pod-image.yml` ran on selected `devel` pushes
or manual dispatches. It watched `x/codex_pod_image/`, `flake.lock`,
`nix/flake/packages.nix`, and its setup files. It ran
`nix build .#codex-pod-image`, then used `skopeo` to push a timestamped `devel-*`
tag to the Forgejo `ducktape-ci/codex-pod` repository. That workflow is retired. The
flake output is retained, but its current evaluation failure is noted above.

## Pieces

- **Image + env**: `x/codex_pod_image/default.nix` — a `buildEnv` tool set on
  `/bin` plus the user's static dotfiles baked from a home-manager config
  (`x/codex_pod_image/home.nix`). No runtime bootstrap script; all static config
  is Nix-built and baked into the image — including Codex's `~/.codex/config.toml`
  (`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`, mirroring
  agent-box's unattended `ducktape.codex`). Baked directly rather than via the
  upstream `programs.codex` module, whose config.toml comes from a home-manager
  _activation_ script that never runs in this activation-less image.
- **Former CI**: see [Historical image build workflow](#historical-image-build-workflow).
- **Former auto-roll**: the `ImageRepository` and `ImagePolicy` have been removed from
  `cluster/k8s/flux-image-automation-forgejo/`; the archived Deployment's image marker
  is no longer updated.
- **Registry credential**: `cluster/k8s/forgejo-images/` provisions the
  `ducktape-ci` Forgejo user (Terraform) and a `forgejo-images-creds` Secret
  reflected into `flux-system` (scan) + `codex-pod` (`imagePullSecrets`).
- **Runtime**: non-root UID 1000. `HOME=/home/codex` (the baked dotfiles, incl.
  the Codex config) is distinct from the work dir: the `codex-workspace` PVC
  (`seaweedfs-ovh`) mounts at `/workspace` so it doesn't shadow the baked home, and
  holds the work tree plus `XDG_CACHE_HOME` (`/workspace/.cache`) for build caches.
  `CODEX_HOME` is left at its default (`~/.codex`, baked); Codex's own state
  (history/sessions) is ephemeral there — durable state is the git work tree on the
  PVC.
- **Access**: `kubectl exec` (env inherited), or `ssh codex-pod` — the container's
  main process is `sshd -D` on `127.0.0.1:2222`, reached via a `kubectl exec` +
  `socat` `ProxyCommand` (baked into `nix/home/home.nix`; no exposed port, no
  Service). kube RBAC gates the transport; sshd adds pubkey auth (workstation keys
  baked into `~/.ssh/authorized_keys`) so ssh-native tooling (rsync/scp/git/VS Code
  Remote) works. The sshd host key persists on the PVC (`/workspace/.sshd`).

## Bring-up (historical — describes the original Flux-wired flow, since retired; see top)

Hosting on our own Forgejo registry means **no manual "make public" step** (the
pull credential is provisioned in code — that's the whole point of the
GHCR→Forgejo move). On merge to `devel`, once re-wired:

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
- **Forgejo API / `tea`** — `forgejo-token-rotation` mints a full-account API
  token for the `codex-pod` Forgejo user and writes a pod-ready Secret
  (`forgejo-tea`) mounted at `/home/codex/.config/tea/config.yml`. Smoke test with
  `tea whoami`. The token does not expand repository permissions; it follows the
  account's existing collaborator grants.
- **BuildBuddy** — `BUILDBUDDY_API_KEY` is set on the container from the
  `buildbuddy-api-key` Secret via `secretKeyRef` (`optional: true`); `bbr`
  reads it. This deployment is parked and is no longer an approved recipient
  of the key. Add an explicit external-creds consumer and ESO source wiring
  before reactivating it.
- **LiteLLM (Codex model)** — Codex routes to LiteLLM's Codex-subscription models,
  served by CLIProxyAPI over a native `/v1/responses` passthrough, instead of an
  interactive ChatGPT sign-in. The baked `~/.codex/config.toml`
  defines a `litellm` `model_provider` (`base_url = litellm.allegedly.works/v1`,
  `wire_api = responses`, `env_key = LITELLM_API_KEY`) and defaults to
  the hidden `gpt-6-astra` alias targeting `chatgpt/oai-responses/gpt-6-astra`,
  which lets Codex load its bundled Astra metadata. `LITELLM_API_KEY` is a dedicated virtual key minted by
  `tf/gitops/litellm-keys` (alias `codex-pod`, scoped to the oai/chatgpt models with
  a budget — deleting it is the kill switch), reflected into `codex-pod` and set on
  the container via `secretKeyRef` (`optional: true`).
- **codex-claude via LiteLLM** — the image includes Claude Code plus the shared
  `codex-claude` wrapper, which points at the main LiteLLM proxy (→ CLIProxyAPI) using a
  scoped `codex-clients` virtual key (`tf/gitops/litellm-keys`). That key is reflected into
  `codex-pod` and exposed as `CODEX_LITELLM_KEY` for both `kubectl exec` and SSH sessions.

## Follow-ups

- **Attic cache (auto-rotated)** — wire `cache.allegedly.works/{main,gaffer}` as
  substituters so the pod's nix/bazel builds reuse our closures. Both caches need a
  reader JWT (`main:r,gaffer:r`); neither is public-read. Do it _right_: extend
  `cluster/rotators/attic_jwt_rotation` (`rotate.py` + `rotators.yaml`) to emit a
  **k8s Secret / netrc** target (today it only writes sops-nix host files and
  overwrites rather than merges), so the pod's reader token auto-rotates. A
  hand-minted static token works but silently expires in ~1 year — rejected. Pod
  side once the rotated secret exists: mount it, add the substituters +
  `trusted-public-keys` (`main:owYQ…`, `gaffer:78zV…`, SSOT `nix/attic-pubkeys.json`)
  - `netrc-file` to the baked `~/.config/nix/nix.conf`.
- **Push-time reconcile — not wired (5m poll is fine)** — a Forgejo `package`
  webhook would give instant pickup instead of the 5m `ImageRepository` poll, but
  there's no clean path: the `svalabs/forgejo` provider only has
  `forgejo_repository_webhook`, while codex-pod's image is a `ducktape-ci` **user**
  package with no repo — so the right primitive (a user/org `package` webhook) has
  no resource. The workarounds are bespoke/fragile: a raw-API `data http` POST
  creating a `ducktape-ci` user webhook (no provider resource ⇒ no drift detection
  or clean delete), or giving `ducktape-ci` a repo + linking the package to it
  (haku's painful path — `cluster/k8s/haku/ui-image-webhook` +
  `tf/gitops/haku-state`, incl. the out-of-band package-link). Not worth it for a
  dev pod; revisit if the provider gains a user/org webhook resource.
- **Generalize** — once proven, move other `ghcr.io/agentydragon` app images to
  Forgejo image-by-image (weigh the per-image pull-availability tradeoff — an
  in-cluster registry outage means those pods can't pull).
