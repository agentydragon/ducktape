# Nix Configuration TODOs

## Roll gnome-remote-desktop RDP (over the Nebula SSH tunnel) to all hosts

wyrm2 now serves a headless GNOME session over gnome-remote-desktop system RDP
("Remote Login"), reached via an SSH-key tunnel on Nebula
(`ssh -L 3390:localhost:3389 <host>` + an RDP client). See
`nix/nixos/hosts/wyrm2/default.nix` (`grdConf` + the `rdp_tls_*` SOPS pair). RDP
is enabled declaratively via a `grd.conf` tmpfiles symlink — not `grdctl`, whose
`rdp enable` can't write to read-only `/etc` on NixOS.

Once proven on wyrm2 — especially the NVIDIA-headless render path tracked by the
`CLEANUP(added 2026-07-17)` tombstone there — roll the same pattern to the other
NixOS hosts (rugged, iguana, atlas, …). Each host needs its own per-host RDP TLS
SOPS pair (admin + `<host>-host`, like the Nebula host keys) and the `grd.conf`
tmpfiles symlink.

## Design the cluster-based Syncthing topology

ActivityWatch participants (`wyrm2`, `iguana`, `rugged`, and `atlas`) now own
a deliberately narrow, Nix-managed Syncthing configuration: each has only its
ActivityWatch send-only folder and the cluster receiver as a peer. The old
general-purpose wyrm2 configuration is backed up locally at
`~/backups/syncthing/2026-07-11-pre-declarative-reset` and is intentionally not
managed or restarted.

Before adding other folders or peers, decide the target cluster topology,
identity ownership, folder ownership, and recovery procedure. Preserve this
one-purpose configuration until that design is settled.

## Consider wiring Codex OTEL export

Codex supports native OTLP exporters via `[otel]` in `config.toml`, including
logs, traces, metrics, static headers, and `log_user_prompt = true`. Consider
wiring this through `nix/home/codex/default.nix` so local Codex sessions export
to `https://alloy-otlp.allegedly.works`.

Current caveat: Codex supports static OTLP headers in config, but not a dynamic
header helper/file like Claude Code's `otelHeadersHelper`. A direct Alloy setup
would need to inject the SOPS-rendered bearer into the activation-generated
`config.toml`, so token rotation requires regenerating that config. Prefer a
dynamic helper/file upstream or a local auth-injecting forwarder if that becomes
annoying.

## Nix PATH not available in non-interactive shells (mosh issue)

**Problem:** On non-NixOS systems with nix installed, mosh fails to find nix-installed `mosh-server` because:

1. mosh runs `mosh-server` via SSH in a **non-login, non-interactive shell**
2. The nix installer only adds PATH setup to:
   - `/etc/profile.d/nix.sh` (login shells via `/etc/profile`)
   - `/etc/zsh/zshrc` (interactive zsh shells)
3. Neither runs for non-interactive shells, so `which mosh-server` finds `/usr/bin/mosh-server` (old system version) instead of `~/.nix-profile/bin/mosh-server`

**Symptoms:**

- mosh true color broken (mosh 1.4.0 required, but system has 1.3.2)
- Any nix-installed binary unavailable when running commands via `ssh host 'command'`

**Current workaround:** Added nix-daemon.sh sourcing to `programs.zsh.envExtra` in home.nix (writes to `~/.zshenv` which runs for ALL zsh invocations).

**Proper fix:** The nix installer should add sourcing to `/etc/zsh/zshenv` instead of/in addition to `/etc/zsh/zshrc`. This is arguably a gap in nix's shell integration.

## Wire wyrm2 GitHub SSH key

wyrm2 is the only desktop host missing `ducktape.githubSsh`. All others
(rugged, atlas, gecko, iguana) have `ssh_keys/<host>-github.sops.key` and
`ducktape.githubSsh.sopsFile` wired. Steps:

1. `ssh-keygen -t ed25519 -f /tmp/wyrm2-github` on wyrm2 (or here)
2. Upload the public key to GitHub → Settings → SSH keys
3. `sops -e -i ssh_keys/wyrm2-github.sops.key` (binary encrypt the private key)
4. Add `ducktape.githubSsh.sopsFile = ../../../ssh_keys/wyrm2-github.sops.key;` to `nix/home/hosts/wyrm2.nix`
5. Import `../modules/github-ssh.nix` in wyrm2.nix (already done via the module option)

## Deduplicate wyrm2/rugged/iguana host configs

wyrm2, rugged, and iguana import nearly identical module sets (gui, workstation, bazel, system-inspection-sudo, k8s-worker) and have similar `k8sWorker` config. Extract common setup into a shared module (e.g., `modules/k8s-workstation.nix`).

## Rugged: evaluate `systemd-homed`

Consider whether `nix/nixos/hosts/rugged` should switch from the current single `cryptroot` layout to a `systemd-homed`-style design for tablet-friendly graphical login and per-user home unlock. Evaluate tradeoffs versus keeping full-disk LUKS and adding a non-keyboard pre-boot unlock path such as FIDO2.

## TTY password feedback

Evaluate whether console TTY password prompts can show visual feedback (for example `*` per keystroke) on NixOS hosts. This is not a standard NixOS knob like `sudo` `pwfeedback`; TTY login goes through `agetty` into `login`/PAM, so this likely requires a downstream package override or alternate login program. Scope and risks need review before implementing.

## Roll out drivefs to remaining hosts

The per-host `cache.allegedly.works/{main,gaffer}` reader credentials and Nix
substituter wiring are present for all supported machines. `drivefs` itself is
enabled on **wyrm2** and **rugged**. Decide whether to enable
`services.google-drive` on **iguana** and **atlas**; atlas is not NixOS, so this
would remain a Home Manager service.

- [ ] Auto-fetch the gaffer pubkey post-cache-creation and PR it into
      `nix/attic-pubkeys.json` (TODO already noted in the module + `bootstrap.sh`).
- [ ] Split the `&ci` age recipient into `&ducktape-ci` and `&gaffer-ci` so the
      gaffer writer token isn't decryptable by ducktape CI's age key (TODO already
      noted in `.sops.yaml`).
