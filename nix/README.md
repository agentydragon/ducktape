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

### NixOS Machines (rugged, wyrm2)

```bash
# System configuration (requires sudo)
sudo nixos-rebuild switch --flake ~/code/ducktape#<hostname>

# User configuration
home-manager switch --flake ~/code/ducktape#<hostname> --impure
```

### Non-NixOS Machines (agentydragon, gpd, vps)

Only home-manager (user config):

```bash
home-manager switch --flake ~/code/ducktape#<hostname> --impure
```

Note: `--impure` is required for nixGL (GPU driver detection).

## Available Hosts

### NixOS System Configs (`nixosConfigurations`)

| Host     | Type     | Description           |
| -------- | -------- | --------------------- |
| `rugged` | Physical | Dell Rugged 12 tablet |
| `wyrm2`  | VM       | Dev workstation VM    |

### Home-Manager Configs (`homeConfigurations`)

| Host           | OS       | Description           |
| -------------- | -------- | --------------------- |
| `agentydragon` | Pop!\_OS | ThinkPad X1 Extreme   |
| `gpd`          | Pop!\_OS | GPD Win Max 2         |
| `rugged`       | NixOS    | Dell Rugged 12 tablet |
| `nixos-vm`     | NixOS    | NixOS VM (wyrm2)      |
| `vps`          | Debian   | VPS server (no GUI)   |

## Common Commands

```bash
# Test build without applying
sudo nixos-rebuild build --flake ~/code/ducktape#<hostname>
home-manager build --flake ~/code/ducktape#<hostname> --impure

# Build from GitHub directly (no local checkout needed)
sudo nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#<hostname>
home-manager switch --flake github:agentydragon/ducktape?ref=devel#<hostname> --impure

# List home-manager generations
home-manager generations

# Rollback home-manager
home-manager rollback
```
