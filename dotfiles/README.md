# dotfiles

**Migrating to Nix home-manager** (see `nix/home/home.nix`). Goal is full NixOS on all machines.

## What's Still Here (pending migration)

| Path          | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `profile`     | PATH modifications, CUDA, lesspipe, machine-specific |
| `config/*`    | App configs not yet migrated to Nix                  |
| `local/bin/*` | Utility scripts                                      |

## What's in Nix Now

Shell configs (`~/.bashrc`, `~/.zshrc`), aliases, environment variables, Powerlevel10k,
GNOME/dconf settings, Git, tmux, SSH/GPG — all in `nix/home/home.nix`.
