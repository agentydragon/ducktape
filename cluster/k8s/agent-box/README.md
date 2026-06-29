# agent-box — self-hosted agent VM

A dedicated, SSH-accessible NixOS VM (`agent-box`) hosting agent users, each under
its own scoped identity. The first user, `codex`, runs the OpenAI Codex CLI so a
ChatGPT-Pro subscription can drive coding/cluster tasks online. Baseline UX:
`ssh codex@agent-box.allegedly.works` (or `ssh agent-box.allegedly.works` from a
host with the matchBlock), attach tmux, drive `codex`.

TOS note: a single-user personal agent on the user's own ChatGPT-Pro sub via the
Codex CLI / `codex exec` / SDK is documented functionality, not a TOS violation
(see `docs/self_hosted_coding_agent_platforms.md`, `docs/ai_subscription_comparison.md`).
Open follow-ups (per-VM SSH, more users, Codex auth provisioning) are tracked in
<../TODO.md> § "agent-box follow-ups".

## Identity model

Two pre-generated ed25519 keypairs become age recipients via `ssh-to-age`:

- **`agent-box-host`** — the VM's persisted SSH host key (installed via cloud-init,
  the gecko pattern). Stable age identity across reboots; decrypts the codex user
  key and host-scoped secrets (attic).
- **`agent-box-codex-user`** — the codex user's `~/.ssh/id_ed25519`, doubling as
  its age decryption identity (same trick as `.envrc`'s `SOPS_AGE_KEY`-from-ed25519).
  Decrypts user-scoped secrets (BuildBuddy, the Forgejo bot key).

Plus **`agent-box-codex-forgejo`** — the codex user's Forgejo git push key (SSH
auth, no age role).

Bootstrap chain (non-circular): cloud-init plants the host key → NixOS sops-nix
decrypts `agent-box-codex-user.sops.key` (via host key) → plants
`/home/codex/.ssh/id_ed25519` (tmpfiles pre-creates the dir codex-owned) →
home-manager sops-nix (user=codex) decrypts BuildBuddy + the Forgejo key off that
id.

You log in _as_ codex with your personal keys (in `authorizedKeys`); the
`agent-box-codex-user` key is the bot's own identity for secrets + git.

## Cloning Forgejo repos as codex

The codex user clones the repos it collaborates on over SSH using the planted bot
key — no netrc/HTTP credential involved:

```bash
git clone git@git.allegedly.works:agentydragon/ducktape.git
git clone git@git.allegedly.works:agentydragon/gaffer-private.git
```

This relies on the `git.allegedly.works` matchBlock (port 2222 + the
`agent-box-codex-forgejo` key) that `nix/home/modules/forgejo-ssh.nix` writes to
`~/.ssh/config`. Forgejo is the working forge for codex (push topic branches, open
PRs there); the Forgejo repos are not formal pull-mirrors of GitHub, so sync them
from GitHub manually when needed.

## First-boot secret ordering (known-ugly, works)

Direct-boot images hit a first-boot race that bootstrap+switch never hits (gecko's
sops runs at `nixos-rebuild switch`, long after cloud-init). Two bugs, both worked
around in `nix/nixos/hosts/agent-box/default.nix`:

1. **system sops vs cloud-init**: `sops-install-secrets` runs early (sysinit) but
   cloud-init writes the persisted host key late (cloud-config stage), so first-boot
   sops can't decrypt `codex_id_ed25519`. Worked around by a `oneshot` after
   `cloud-final.service` that restarts sops + home-manager.
2. **headless home-manager sops**: user-level secrets install via the codex user's
   `sops-nix.service` (`systemd --user`), which never starts without a session.
   Worked around with `users.users.codex.linger = true`.

## Deploy

- Build/refresh the bootstrap qcow2 (`cluster/k8s/vm-images-publisher/`) if a newer
  base is wanted; the VM currently pins the same SHA as gecko.
- Boot the VM, then `nixos-rebuild switch ...#agent-box` (pause before activating
  per house rule).
- First `forgejo-agentydragon-repos` reconcile mutates the real Forgejo repos
  (adopt + collaborators) — watch the initial Terraform plan/apply. Settings were
  matched to the live repos to keep the import diff-free; collaborator grants are
  additive.

## See also

- <../../../nix/nixos/hosts/agent-box/default.nix> — NixOS host config
- <app/virtualmachine.yaml> — KubeVirt VM definition
- <../agents/agent-rbac-base/README.md> § "agent-box Codex" — cluster RBAC for the
  codex user
- Per-VM SSH beyond a few VMs: <../../docs/plans/vm_ssh_exposure.md>
