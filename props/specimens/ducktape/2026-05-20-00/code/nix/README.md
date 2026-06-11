# Nix Configuration

NixOS and home-manager configurations. The flake is at the repo root (`flake.nix`).

## Directory Structure

```
nix/
├── nixos/         # NixOS system configurations
│   ├── modules/   # Shared NixOS modules
│   └── hosts/     # Per-host system config
├── home/          # home-manager user configurations
│   ├── home.nix   # Shared home-manager config
│   ├── hosts/     # Per-host home config
│   └── packages/  # Custom Nix packages
└── TODO.md        # Future improvements
```

## Usage

All commands run from `~/code/ducktape/`.

### NixOS Hosts (iguana, rugged, wyrm2)

These hosts inline home-manager through the NixOS system configuration, so a
single `nixos-rebuild` applies both system and user config.

```bash
# System + inline home-manager configuration (requires sudo)
sudo nixos-rebuild switch --flake ~/code/ducktape#<hostname>
```

### Standalone Home-Manager Configs (atlas, nixos-vm)

These are the current standalone `homeConfigurations` exposed by the flake:

```bash
home-manager switch --flake ~/code/ducktape#nixos-vm
home-manager switch --impure --flake ~/code/ducktape#atlas
```

Note: `atlas` needs `--impure` because it uses nixGL on a non-NixOS system.

## Available Hosts

### NixOS System Configs (`nixosConfigurations`)

| Host        | Type     | Description                  |
| ----------- | -------- | ---------------------------- |
| `iguana`    | Physical | ThinkPad X1 Extreme          |
| `rugged`    | Physical | Dell Rugged 12 tablet        |
| `wyrm2`     | VM       | Dev workstation VM (Proxmox) |
| `bootstrap` | VM/Image | Minimal bootstrap image      |

### Home-Manager Configs (`homeConfigurations`)

| Host       | OS       | Description                     |
| ---------- | -------- | ------------------------------- |
| `atlas`    | Proxmox  | Proxmox VE host home config     |
| `nixos-vm` | NixOS VM | Simplified standalone HM config |

## Common Commands

```bash
# Test NixOS host build without applying
sudo nixos-rebuild build --flake ~/code/ducktape#<hostname>

# Test standalone home-manager config without applying
home-manager build --flake ~/code/ducktape#nixos-vm
home-manager build --impure --flake ~/code/ducktape#atlas

# Apply a NixOS host directly from GitHub (system + inline HM)
sudo nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#<hostname>

# Apply a standalone home-manager config directly from GitHub
home-manager switch --flake github:agentydragon/ducktape?ref=devel#nixos-vm
home-manager switch --impure --flake github:agentydragon/ducktape?ref=devel#atlas

# List standalone home-manager generations
home-manager generations

# Roll back a standalone home-manager config
home-manager rollback
```
