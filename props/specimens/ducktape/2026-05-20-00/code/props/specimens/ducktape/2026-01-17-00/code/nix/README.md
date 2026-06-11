# Nix Configuration

Nix flakes for system (NixOS) and user (home-manager) configuration.

## Directory Structure

```
nix/
├── nixos/     # NixOS system configurations (flake)
├── home/      # home-manager user configurations (flake)
└── TODO.md    # Future improvements
```

## Usage

### NixOS Machines (rugged, wyrm2)

Two commands - system config and user config are managed separately:

```bash
# System configuration (requires sudo)
cd ~/code/ducktape/nix/nixos
sudo nixos-rebuild switch --flake .#<hostname>

# User configuration
cd ~/code/ducktape/nix/home
home-manager switch --flake .#<hostname> --impure
```

### Non-NixOS Machines (agentydragon, gpd, vps)

Only home-manager (user config):

```bash
cd ~/code/ducktape/nix/home
home-manager switch --flake .#<hostname> --impure
```

Note: `--impure` is required for nixGL (GPU driver detection).

## Available Hosts

### NixOS System Configs (`nix/nixos`)

| Host     | Type     | Description           |
| -------- | -------- | --------------------- |
| `rugged` | Physical | Dell Rugged 12 tablet |
| `wyrm2`  | VM       | Dev workstation VM    |

### Home-Manager Configs (`nix/home`)

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
sudo nixos-rebuild build --flake .#<hostname>
home-manager build --flake .#<hostname> --impure

# Build from GitHub directly (no local checkout needed)
sudo nixos-rebuild switch --flake github:agentydragon/ducktape?dir=nix/nixos&ref=devel#<hostname>
home-manager switch --flake github:agentydragon/ducktape?dir=nix/home&ref=devel#<hostname> --impure

# List home-manager generations
home-manager generations

# Rollback home-manager
home-manager rollback
```
