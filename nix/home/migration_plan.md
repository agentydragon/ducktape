# Nix Home-Manager Migration Status

## Current State

Nix home-manager (flakes, nixpkgs 25.11) manages user-level configuration.
NixOS hosts inline home-manager through `nixos-rebuild`; standalone
`homeConfigurations` remain for non-NixOS and test-only hosts. Ansible handles
system-level setup on non-NixOS machines (apt packages, services, udev rules).

### Deployment

For NixOS hosts (`iguana`, `rugged`, `wyrm2`, `gecko`):

```bash
sudo nixos-rebuild switch --flake ~/code/ducktape#<hostname>
```

For standalone home-manager configs:

```bash
home-manager switch --flake ~/code/ducktape#nixos-vm
home-manager switch --impure --flake ~/code/ducktape#atlas
```

### Per-Host Status

Ansible-role columns cross-checked against the playbooks (`ansible/{gpd,vps,atlas}.yaml`)
and `ansible/roles/`.

| Host         | Nix status          | Ansible roles                                 | Notes                                           |
| ------------ | ------------------- | --------------------------------------------- | ----------------------------------------------- |
| **iguana**   | Inline via NixOS    | (NixOS-managed, no Ansible playbook)          | Fully NixOS (was Pop!\_OS "agentydragon")       |
| **rugged**   | Inline via NixOS    | (NixOS-managed, no Ansible playbook)          | Fully NixOS tablet                              |
| **wyrm2**    | Inline via NixOS    | (NixOS-managed, no Ansible playbook)          | Fully NixOS VM                                  |
| **gecko**    | Inline via NixOS    | (NixOS-managed, no Ansible playbook)          | Headless CLI-only VM (Proxmox) for agents       |
| **atlas**    | Standalone HM entry | `system_inspection_nopasswd`, `nebula`, `nix` | Minimal Proxmox host                            |
| **nixos-vm** | Standalone HM entry | (test-only config, no Ansible playbook)       | Simplified standalone home-manager config       |
| **gpd**      | No current output   | `gui`, `legacy_gui`, `laptop`                 | Still uses the `legacy_gui` role — last holdout |
| **vps**      | No current output   | `common`, `system_inspection_nopasswd`        | Server, no GUI/dev tools                        |

### What Nix Manages

- User packages (CLI tools, dev tools, GUI apps, fonts)
- Shell configuration (zsh, bash, atuin, direnv, zoxide, eza)
- Shell aliases and environment variables
- GNOME/dconf settings, autostart entries
- Git, readline, tmux, dircolors config
- Bazel user config (`.bazelrc`)
- SSH/GPG agent services
- Claude Code MCP server configuration
- 2 critical MIME associations via `home.activation.fixMimeApps`

### What Ansible Still Manages

- System packages via apt
- Docker daemon
- Legacy VPN uninstall (removes old Tailscale/WireGuard)
- udev rules (GPD trackpoint quirk)
- System-level services

## Remaining Work

### GPD Migration

GPD is the last host using the `legacy_gui` role. It duplicates what Nix already
provides (GUI apps, GNOME extensions, nerd fonts). Once GPD runs `home-manager
switch`, remove the legacy role from `gpd.yaml` and delete:

- `ansible/roles/legacy_gui`

### Not Yet Migrated to Home-Manager

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
# Standalone home-manager configs
home-manager generations
home-manager rollback

# NixOS hosts with inline home-manager
sudo nixos-rebuild switch --rollback
```
