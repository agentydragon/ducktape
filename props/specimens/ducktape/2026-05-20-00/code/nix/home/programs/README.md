# Generic Home-Manager Modules

This directory contains **generic home-manager-style modules** that follow upstream conventions and could potentially be contributed to nixpkgs.

## Design Principles

- **No personal configuration embedded** - modules expose options, personal integrations provide values
- **Upstream-compatible conventions** - follow nixpkgs `programs.*` patterns
- **Declarative configuration** - handle settings files, policy generation, and deployment
- **Well-documented options** - clear descriptions, examples, and type definitions

## Current Modules

- **`gemini-cli.nix`**: Declarative configuration for Gemini CLI
  - Deploys `settings.json` (JSON)
  - Generates policy files (TOML)
  - Deploys shared skills to `~/.gemini/skills/`

## Usage Pattern

Generic modules are consumed by personal integration files (e.g., `../gemini_cli.nix`):

```nix
# Personal integration (../gemini_cli.nix)
{
  programs.gemini-cli = {
    enable = true;
    settings = {
      general.defaultApprovalMode = "default";
      ui.theme = "dark";
    };
    policies = {
      allowed-commands = [
        { toolName = "run_shell_command"; commandPrefix = "git status"; decision = "allow"; priority = 300; }
      ];
    };
  };
}
```

The generic module handles all the deployment mechanics (file generation, path management, etc.).
