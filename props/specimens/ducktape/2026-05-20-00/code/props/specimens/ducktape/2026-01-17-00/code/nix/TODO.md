# Nix Configuration TODOs

## Consider unified NixOS + home-manager management

Currently using two separate commands:

- `sudo nixos-rebuild switch --flake .#<host>` for system config
- `home-manager switch --flake .#<host>` for user config

Could unify via `home-manager.nixosModules.home-manager` to use single `nixos-rebuild` command.

**Tradeoffs:**

- Unified: Single command, atomic updates, guaranteed consistency
- Separate: No sudo for user changes, same home config works on NixOS and non-NixOS machines
