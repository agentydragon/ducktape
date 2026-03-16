# Nix Home-Manager Migration Status

## Current State

Nix home-manager (flakes, nixpkgs 25.11) manages user-level configuration.
Ansible handles system-level setup (apt packages, services, udev rules).

### Deployment

```bash
home-manager switch --flake ~/code/ducktape#<hostname>
```

### Per-Host Status

| Host             | Nix                | Ansible roles                                                                        | Notes                                              |
| ---------------- | ------------------ | ------------------------------------------------------------------------------------ | -------------------------------------------------- |
| **agentydragon** | Full               | `nix`, `docker`, `system_inspection_nopasswd`, `tailscale_client`, `k3s_client`      | `cli`/`gui` commented out — Nix handles everything |
| **wyrm**         | Full               | `cli`, `dev_env`, `golang`, `docker`, `gui`, `tailscale_client`, `k3s_client`        | Ansible for system packages only                   |
| **atlas**        | Full               | `cli`, `nix`, `system_inspection_nopasswd`, `tailscale_client`                       | Minimal Proxmox host                               |
| **gpd**          | Flake entry exists | `cli`, `legacy_cli`, `gui`, `legacy_gui`, `laptop`, `tailscale_client`, `k3s_client` | Still uses legacy roles — last holdout             |
| **vps**          | Flake entry exists | `common`, `system_inspection_nopasswd`                                               | Server, no GUI/dev tools                           |

### What Nix Manages

- User packages (CLI tools, dev tools, GUI apps, fonts)
- Shell configuration (zsh, bash, atuin, direnv, zoxide, eza)
- Shell aliases and environment variables
- GNOME/dconf settings, autostart entries
- Git, readline, tmux, dircolors config
- Bazel user config (`.bazelrc`)
- SSH/GPG agent services
- Claude Code MCP server configuration (via claude-code-router module)
- 2 critical MIME associations via `home.activation.fixMimeApps`

### What Ansible Still Manages

- System packages via apt
- Docker daemon
- Tailscale/headscale client
- k3s client + kubeconfig
- udev rules (GPD trackpoint quirk)
- System-level services
- Dotfiles via rcm (`.profile`, application configs not yet in Nix)

## Remaining Work

### GPD Migration

GPD is the last host using `legacy_cli` and `legacy_gui` roles. These legacy roles
duplicate what Nix already provides (pipx tools, nodejs, rust, neovim, bazel, CLI
utils, GNOME extensions, nerd fonts). Once GPD runs `home-manager switch`, remove
the legacy roles from `gpd.yaml` and delete:

- `ansible/roles/legacy_cli`
- `ansible/roles/legacy_gui`
- `ansible/roles/legacy_nerd_fonts`
- `ansible/roles/legacy_claude_mcp`

### Still in Ansible/rcm (Not Yet Migrated)

- **`~/.profile`** — conditional PATH management, CUDA, machine-specific config
- **`~/.config/*`** — application configs not yet in Nix
- **`~/.local/bin/*`** — utility scripts
- **NPM global packages** (jscpd, madge) — not in nixpkgs, must install manually with `pnpm add -g jscpd madge`

## Key Learnings

### From K8s Sandbox Testing

1. **Python version**: Use Python 3.12, not 3.13 (numpy compatibility)
2. **Don't install home-manager via nix-env** — let `programs.home-manager.enable = true` handle it
3. **File conflicts**: May need to remove existing files before first activation
4. **dbus issues**: Expected in containers, won't affect real systems

### From Atlas Deployment (Debian/Proxmox)

1. **Shell initialization**: Debian's `/etc/profile` resets PATH unconditionally, breaking Nix paths — don't source `/etc/profile` in `.shellrc`
2. **Installation cleanup**: Failed root installations leave artifacts (backup files, nixbld users/groups, `/nix` directory) that block reinstalls
3. **Multi-user setup**: Requires `NIX_REMOTE=daemon` for proper operation

## Rollback

```bash
home-manager generations  # List generations
home-manager rollback     # Go to previous
```
