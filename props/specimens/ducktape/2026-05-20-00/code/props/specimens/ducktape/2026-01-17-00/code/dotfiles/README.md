# dotfiles

**Most shell configuration has migrated to Nix home-manager** (see `nix/home/home.nix`).

Remaining dotfiles are managed with [rcm](https://github.com/thoughtbot/rcm), deployed by Ansible.

## What's Still Here

| Path          | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `profile`     | PATH modifications, CUDA, lesspipe, machine-specific |
| `config/*`    | App configs not yet migrated to Nix                  |
| `local/bin/*` | Utility scripts                                      |
| `host-*/`     | Host-specific rcm overrides                          |

## What's in Nix Now

Shell configs (`~/.bashrc`, `~/.zshrc`), aliases, environment variables, Powerlevel10k - all in `nix/home/home.nix`.

See `docs/shell-configuration.md` for migration status and loading order.

## User Scripts (.local/bin)

Utility scripts symlinked to `~/.local/bin/`:

- Theme switchers (`set_dark_theme`, `set_light_theme`)
- Backup utilities (`duplicity`)
- Git utilities (`git-purge-file`)

## Commands

```bash
lsrc                    # List managed files
mkrc ~/.tigrc           # Add new RC file
rcup -B agentydragon    # Update symlinks
```
