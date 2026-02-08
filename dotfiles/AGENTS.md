@README.md

# Agent Guide for `dotfiles/`

## Important Rules

- **Most shell config is in Nix** — see `nix/home/home.nix`, not here
- **DO NOT edit `~/.bashrc`, `~/.zshrc` directly** — they're Nix-generated
- **For aliases/env vars**: Edit `home.shellAliases` or `home.sessionVariables` in Nix
- **For PATH/conditionals**: Edit `dotfiles/profile` (still rcm-managed)

## Shell Configuration

See `docs/shell_configuration.md` for migration status and loading order.
