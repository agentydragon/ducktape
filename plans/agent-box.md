# agent-box — self-hosted agent VM (codex user)

Goal: a dedicated, SSH-accessible NixOS VM (`agent-box`) hosting agent users, each
under its own scoped identity. The first user, `codex`, runs the OpenAI Codex CLI so
the ChatGPT-Pro subscription can drive coding / cluster tasks online. Baseline UX:
`ssh codex@agent-box.allegedly.works` (or `ssh agent-box.allegedly.works` from a host
with the matchBlock), attach tmux, drive `codex`. A web UI is a later layer (see
<docs/self_hosted_coding_agent_platforms.md>).

TOS note: a single-user personal agent on the user's own ChatGPT-Pro sub via the Codex
CLI / `codex exec` / SDK is documented functionality, not a TOS violation (see
<docs/ai_subscription_comparison.md> TOS notes).

## Identity model

Two pre-generated ed25519 keypairs become age recipients via `ssh-to-age`:

- **`agent-box-host`** — the VM's persisted SSH host key (installed via cloud-init, the
  gecko pattern). Stable age identity across reboots; decrypts the codex user key and
  host-scoped secrets (attic).
- **`agent-box-codex-user`** — the codex user's `~/.ssh/id_ed25519`, doubling as its age
  decryption identity (same trick as `.envrc`'s `SOPS_AGE_KEY`-from-ed25519). Decrypts
  user-scoped secrets (BuildBuddy, the Forgejo bot key).

Plus **`agent-box-codex-forgejo`** — the codex user's Forgejo git push key (SSH auth, no
age role).

Bootstrap chain (non-circular): cloud-init plants the host key → NixOS sops-nix decrypts
`agent-box-codex-user.sops.key` (via host key) → plants `/home/codex/.ssh/id_ed25519`
(tmpfiles pre-creates the dir codex-owned) → home-manager sops-nix (user=codex) decrypts
BuildBuddy + the Forgejo key off that id.

You log in _as_ codex with your personal keys (in `authorizedKeys`); the
`agent-box-codex-user` key is the bot's own identity for secrets + git.

## What's built (this PR)

- **Keys + SOPS**: `ssh_keys/agent-box-{host,codex-user,codex-forgejo}.{pub,sops.key}`;
  `.sops.yaml` anchors `&agent-box-host` / `&agent-box-codex-user`, rules for the key
  files, `secrets/hosts/agent-box-*`, and `*agent-box-codex-user` granted on
  `secrets/buildbuddy.yaml`.
- **NixOS host**: `nix/nixos/hosts/agent-box/`, `nix/home/hosts/agent-box.nix` (slim,
  least-privilege HM: codex CLI + BuildBuddy + Forgejo key + direnv), flake host
  `agent-box` (username `codex`). Evaluates clean.
- **KubeVirt**: `cluster/k8s/agent-box/` (clone of gecko). Public SSH via a
  CiliumEnvoyConfig on port **2201** (gecko owns `:22` on hil), DNS
  `agent-box.allegedly.works`.
- **Forgejo (tofu)**: `tf/gitops/forgejo-codex/` + `cluster/k8s/forgejo/codex/` — creates
  the `codex` Forgejo user + SSH key, adopts `agentydragon/{ducktape,gaffer-private}` via
  `import {}`, grants codex `write`, and protects `devel`/`main` with a push whitelist of
  `agentydragon` (codex must PR; agentydragon keeps direct push).
- **SSH convenience**: `programs.ssh.matchBlocks."agent-box.allegedly.works"` in
  agentydragon's home.nix (user codex, port 2201).
- **Attic rotator entry**: `rotators.json` mints `secrets/hosts/agent-box-attic.yaml`
  after merge.

## Deferred (tombstoned in-code)

- **Attic substituter** on agent-box: the Nix wiring is tombstoned in
  `nix/nixos/hosts/agent-box/default.nix` + `nix/home/hosts/agent-box.nix`. Enable it once
  the attic-jwt-rotation CronJob has minted+committed `secrets/hosts/agent-box-attic.yaml`
  to devel (the path literal would otherwise fail flake eval). A one-line follow-up.

## Gated / external steps (not in the PR)

- Build/refresh the bootstrap qcow2 (`cluster/k8s/vm-images-publisher/`) if a newer base
  is wanted; the VM currently pins the same SHA as gecko.
- Deploy: boot the VM, then `nixos-rebuild switch ...#agent-box` (pause before activating
  per house rule).
- First `forgejo-codex` reconcile mutates the real Forgejo repos (adopt + protect) — watch
  the initial Terraform plan/apply. Settings were matched to the live repos to keep the
  import diff-free; branch protection + collaborators are additive.
- No GitHub identity for codex in v1 (Forgejo-primary). The existing `agentydragon-agent`
  machine user already gets PR-but-not-push for free via the GitHub ruleset if wired later.

## First-boot secret ordering (known-ugly, works)

Direct-boot images have a first-boot race that bootstrap+switch never hits (gecko's sops
runs at `nixos-rebuild switch`, long after cloud-init). Two bugs, both fixed in
`nix/nixos/hosts/agent-box/default.nix` with localized workarounds:

1. **system sops vs cloud-init**: `sops-install-secrets` runs early (sysinit) but
   cloud-init writes the persisted host key late (cloud-config stage), so the first-boot
   sops can't decrypt `codex_id_ed25519`. Worked around by a `oneshot` after
   `cloud-final.service` that restarts sops + home-manager.
2. **headless home-manager sops**: the user-level secrets install via the codex user's
   `sops-nix.service` (a `systemd --user` unit), which never starts without a session.
   Worked around with `users.users.codex.linger = true`.

**TODOs — do better:**

- Deliver the host key _before_ sysinit (cloud-init `bootcmd` in the `cloud-init-local`
  stage, or initrd) so system sops decrypts on the first try — removes the re-run oneshot.
- Find a way for home-manager sops to install user secrets without a lingering session
  (activation-time install rather than a user service), so `linger` isn't needed.
- Or decide direct-boot isn't worth it for sops-bearing hosts and use bootstrap+switch.

## Future

- **More agent users on agent-box**: `claude`, `z-claude` — each with its own login keys,
  `agent-box-<user>-user` age identity, scoped secrets, and home dir. TODO markers in
  `flake.nix` and `nix/nixos/hosts/agent-box/default.nix`.
- **SSH exposure**: per-VM ports get clunky past ~2-3 VMs; see plans/vm_ssh_exposure.md for
  the SNI-multiplexed path.
