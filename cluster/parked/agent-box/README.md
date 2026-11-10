# agent-box — self-hosted agent VM

A dedicated, SSH-accessible NixOS VM (`agent-box`) hosting agent users, each under
its own scoped identity. The first user, `codex`, runs the OpenAI Codex CLI. Its
Forgejo and Authentik service accounts and their repository/Kubernetes access
are retired by this configuration; this VM remains parked. Baseline UX:
`ssh codex@agent-box.allegedly.works` (or `ssh agent-box.allegedly.works` from a
host with the matchBlock), attach tmux, drive `codex`.

TOS note: a single-user personal agent on the user's own ChatGPT-Pro sub via the
Codex CLI / `codex exec` / SDK is documented functionality, not a TOS violation
(see [self-hosted agent platform notes](../../../docs/self_hosted_coding_agent_platforms.md) and [the subscription comparison](../../../x/ai_sub_value/subscription_comparison.md)).
Open follow-ups (per-VM SSH, more users, Codex auth provisioning) are tracked in
<../../k8s/TODO.md> § "agent-box follow-ups".

## Identity model

Two pre-generated ed25519 keypairs become age recipients via `ssh-to-age`:

- **`agent-box-host`** — the VM's persisted SSH host key (installed via cloud-init,
  the gecko pattern). Stable age identity across reboots; decrypts the codex user
  key and host-scoped secrets (attic).
- **`agent-box-codex-user`** — the codex user's `~/.ssh/id_ed25519`, doubling as
  its age decryption identity (same trick as `.envrc`'s `SOPS_AGE_KEY`-from-ed25519).
  It remains for the parked VM's user-scoped secrets, including BuildBuddy and the
  shared LiteLLM Codex key.

Bootstrap chain (non-circular): cloud-init plants the host key → NixOS sops-nix
decrypts `agent-box-codex-user.sops.key` (via host key) → plants
`/home/codex/.ssh/id_ed25519` (tmpfiles pre-creates the dir codex-owned) →
home-manager sops-nix (user=codex) decrypts its remaining user-scoped secrets off
that id.

You log in _as_ codex with your personal keys (in `authorizedKeys`); the
`agent-box-codex-user` key remains the codex user's identity for the separate
parked VM secrets.

## Retired Forgejo and Kubernetes access

The `agent-box-codex` Forgejo and Authentik accounts are retired by this
configuration, along with their Forgejo SSH/API credentials and the
Authentik-issued Kubernetes JWT. The parked home-manager config no longer
installs Forgejo or Kubernetes credentials. The VM's host and user age identities
remain for its separate configuration and secrets.

## First-boot secret ordering (known-ugly, works)

Direct-boot images hit a first-boot race that bootstrap+switch never hits (gecko's
sops runs at `nixos-rebuild switch`, long after cloud-init). Two bugs, both worked
around in `nix/nixos.nix`:

1. **system sops vs cloud-init**: `sops-install-secrets` runs early (sysinit) but
   cloud-init writes the persisted host key late (cloud-config stage), so first-boot
   sops can't decrypt `codex_id_ed25519`. Worked around by a `oneshot` after
   `cloud-final.service` that restarts sops + home-manager.
2. **headless home-manager sops**: user-level secrets install via the codex user's
   `sops-nix.service` (`systemd --user`), which never starts without a session.
   Worked around with `users.users.codex.linger = true`.

## Historical deployment notes

This directory is archival. Its CDI image and credential point at the retired
`vm-images` SeaweedFS bucket. Port the VM to an OCI `containerDisk` image before
adding it back to Flux; see <../../docs/kubevirt_vm_image_artifacts.md>.

- The former flow booted the VM, then ran
  `nixos-rebuild switch ...#agent-box` (pause before activating per house rule).
- Its first `forgejo-agentydragon-repos` reconcile mutates the real Forgejo repos
  (adopt + collaborators) — watch the initial Terraform plan/apply. Settings were
  matched to the live repos to keep the import diff-free; collaborator grants are
  additive.

## See also

- <nix/nixos.nix> — NixOS host config
- <virtualmachine.yaml> — KubeVirt VM definition
- <../../docs/agent_rbac.md> — current cluster RBAC for agent identities
- Per-VM SSH beyond a few VMs: <../../docs/plans/vm_ssh_exposure.md>
