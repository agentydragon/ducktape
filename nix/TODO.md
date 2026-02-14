# Nix Configuration TODOs

## Consider unified NixOS + home-manager management

Currently using two separate commands:

- `sudo nixos-rebuild switch --flake .#<host>` for system config
- `home-manager switch --flake .#<host>` for user config

Could unify via `home-manager.nixosModules.home-manager` to use single `nixos-rebuild` command.

**Tradeoffs:**

- Unified: Single command, atomic updates, guaranteed consistency
- Separate: No sudo for user changes, same home config works on NixOS and non-NixOS machines

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
