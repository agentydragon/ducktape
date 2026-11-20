# Home-Manager Configuration

Home-manager user configurations for multiple machines.

## Directory Structure

```text
nix/home/
├── modules/               # Custom modules (solarized, gnome-shell-keybindings, etc.)
├── tests/                 # Nix tests for modules and integrations
├── allowed-commands.nix   # Personal SSOT: allowed bash commands
├── mcp-servers.nix        # Personal SSOT: remote MCP servers wired into every agent harness
├── skills.nix             # Shared skill deployment helper
├── claude_code/           # Personal integration: Claude Code
├── codex/                 # Personal integration: Codex
├── opencode/              # Personal integration: OpenCode
├── crush/                 # Personal integration: Crush
├── config/, services/, shell/, terminals/   # Neovim config, user services, shell init, terminal setup
├── home.nix               # Main home-manager configuration
└── hosts/*.nix            # Host-specific configurations
```

## Architecture: Personal Integrations

Each agent harness has a **personal integration** (`claude_code/`, `codex/`, `opencode/`, `crush/`) that:

- Transforms shared data sources (like `allowed-commands.nix`) into the harness's configuration
- Wires up personal settings and preferences

### Single Source of Truth (SSOT) Files

- **`allowed-commands.nix`**: Allowed bash commands for Claude Code and Codex execpolicy
  - Shared between personal integrations
  - Defines command permissions used by both agent integrations

- **`skills.nix`**: Shared skill deployment helper for Claude Code, Codex, and OpenCode
  - Expands the CI-built `skills-tar` into each tool's config home under `skills/`
  - Codex uses directory symlinks for skills because its current loader skips symlinked `SKILL.md` files

Shared data used by both home-manager and NixOS lives in `../lib/`:

- **`inspection-commands.nix`**: System inspection commands requiring sudo

## Usage

See <../README.md> for deployment commands.
