# Nix Configuration TODOs

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

**Reported problem:** On non-NixOS systems with Nix installed, mosh fails to find
Nix-installed `mosh-server` because:

1. mosh runs `mosh-server` via SSH in a **non-login, non-interactive shell**
2. The nix installer only adds PATH setup to:
   - `/etc/profile.d/nix.sh` (login shells via `/etc/profile`)
   - `/etc/zsh/zshrc` (interactive zsh shells)
3. Neither runs for non-interactive shells, so `which mosh-server` finds `/usr/bin/mosh-server` (old system version) instead of `~/.nix-profile/bin/mosh-server`

**Symptoms:**

- mosh true color broken (mosh 1.4.0 required, but system has 1.3.2)
- Any nix-installed binary unavailable when running commands via `ssh host 'command'`

**Current repository state:** The previous workaround claim is stale. The shared shell
module currently sets `programs.zsh.envExtra` only to `skip_global_compinit=1`; no
`nix-daemon.sh` sourcing is present in the repository. The non-interactive mosh behavior
has not been rechecked on an affected host. Reproduce it before choosing a configuration
or installer fix.

**Possible fix:** The Nix installer could add sourcing to `/etc/zsh/zshenv` instead of/in
addition to `/etc/zsh/zshrc`. Confirm the cause and scope before treating this as an
installer defect.

## Wire wyrm2 GitHub SSH key

wyrm2 is the only desktop host missing `ducktape.githubSsh`. All others
(rugged, atlas, gecko, iguana) have `ssh_keys/<host>-github.sops.key` and
`ducktape.githubSsh.sopsFile` wired. Steps:

1. `ssh-keygen -t ed25519 -f /tmp/wyrm2-github` on wyrm2 (or here)
2. Upload the public key to GitHub → Settings → SSH keys
3. `sops -e -i ssh_keys/wyrm2-github.sops.key` (binary encrypt the private key)
4. Add `ducktape.githubSsh.sopsFile = ../../../ssh_keys/wyrm2-github.sops.key;` to `nix/home/hosts/wyrm2.nix`

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
      `nix/attic-pubkeys.json`.
- [ ] Split the `&ci` age recipient into `&ducktape-ci` and `&gaffer-ci` so the
      gaffer writer token isn't decryptable by ducktape CI's age key.

- [ ] Move Wyrm2 `/home` to a separate VM volume during a convenient maintenance
      window. Preserve existing home data and define a rollback path; account for
      GNOME session state, mount ordering, and services that use the current home.

## Bring readline, `gh` and git-worktree-switcher config under Home Manager

- readline: `programs.readline` (`nix/home/modules/shell.nix`) sets only `show-all-if-ambiguous`; fold any other
  `~/.inputrc` tweaks into it.
- `gh`: `nix/home/home.nix` installs the package without config. Consider `programs.gh` for settings and extensions,
  and for auth/hosts if feasible.
- git-worktree-switcher: absent from `nix/`. Find the current workflow (scripts? aliases?), migrate its settings,
  document best practices.
