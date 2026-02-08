# dotfiles

**Most shell configuration has migrated to Nix home-manager** (see `nix/home/home.nix`).

Remaining dotfiles are managed with [rcm](https://github.com/thoughtbot/rcm), deployed by Ansible.

## What's Still Here

| Path          | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `profile`     | PATH modifications, CUDA, lesspipe, machine-specific |
| `config/*`    | App configs not yet migrated to Nix                  |
| `local/bin/*` | Utility scripts                                      |

## What's in Nix Now

Shell configs (`~/.bashrc`, `~/.zshrc`), aliases, environment variables, Powerlevel10k - all in `nix/home/home.nix`.

See `docs/shell_configuration.md` for migration status and loading order.

## Commands

```bash
lsrc                    # List managed files
mkrc ~/.tigrc           # Add new RC file
rcup -B agentydragon    # Update symlinks
```
