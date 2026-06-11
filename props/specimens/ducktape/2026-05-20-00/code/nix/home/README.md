# Home-Manager Configuration

Home-manager user configurations for multiple machines.

## Directory Structure

```
nix/home/
├── programs/              # Generic home-manager-style modules (could be upstreamed)
├── modules/               # Custom modules (solarized, gnome-shell-keybindings, etc.)
├── skills/                # Shared skills for Claude Code, Codex, Gemini CLI, OpenCode
├── tests/                 # Nix tests for modules and integrations
├── allowed-commands.nix   # Personal SSOT: allowed bash commands
├── claude_code/            # Personal integration: Claude Code
├── gemini_cli.nix         # Personal integration: Gemini CLI
├── home.nix               # Main home-manager configuration
└── hosts/*.nix            # Host-specific configurations
```

## Architecture: Generic Modules vs Personal Integrations

This directory follows a pattern separating **generic modules** from **personal integrations**:

### Generic Modules (`programs/`)

Generic home-manager-style modules that follow upstream conventions and could potentially be contributed to nixpkgs. See <programs/README.md> for details.

- **No personal configuration embedded** - modules expose options, personal integrations provide values
- **Upstream-compatible conventions** - follow nixpkgs `programs.*` patterns
- **Well-documented options** - clear descriptions, examples, and type definitions

Current modules:

- `gemini-cli.nix` - Declarative configuration for Gemini CLI

### Personal Integrations (root `*.nix` files)

Files at the root level (`claude_code/`, `gemini_cli.nix`, etc.) are **personal integrations** that:

- Transform shared data sources (like `allowed-commands.nix`) into module configurations
- Wire up personal settings and preferences
- Connect multiple generic modules together

Example:

```nix
# gemini_cli.nix - personal integration
let
  allowed = import ./allowed-commands.nix;
in {
  programs.gemini-cli = {
    enable = true;
    policies.allowed-commands = allowed.prefixCommands ++ allowed.exactCommands;
  };
}
```

### Single Source of Truth (SSOT) Files

- **`allowed-commands.nix`**: Allowed bash commands for Claude Code, Gemini CLI, and Codex execpolicy
  - Shared between personal integrations
  - Defines command permissions used by all three agent integrations

- **`skills.nix`**: Shared skill deployment helper for Claude Code, Codex, Gemini CLI, and OpenCode
  - Expands the CI-built `skills-tar` into each tool's config home under `skills/`
  - Codex uses directory symlinks for skills because its current loader skips symlinked `SKILL.md` files

Shared data used by both home-manager and NixOS lives in `../lib/`:

- **`inspection-commands.nix`**: System inspection commands requiring sudo

## Usage

See <../README.md> for deployment commands.
