# AI Subscription Quota Trackers — Research Notes

## GNOME Shell Extensions

- **Claude Code Usage** (<https://extensions.gnome.org/extension/9231/claude-code-usage/>,
  source <https://github.com/xguitoux/ClaudeCodeUsage-GnomeExtension>): shows **Claude Code**
  5-hour and 7-day token quota percentages in the GNOME top bar, from local Claude Code
  usage data (not live Anthropic API calls). The most polished option. Could be added to
  `nix/home/home.nix` via `programs.gnome-shell.extensions`
  (`{ package = pkgs.gnomeExtensions.claude-code-usage; }`). Check
  `nix search nixpkgs claude-code-usage` first; if it is not in `nixpkgs`, package it
  manually via `pkgs.buildEnv` / `fetchFromGitHub` in an overlay.
- **brainusage** (<https://github.com/AltairInglorious/brainusage>): tracks both **Claude
  (Anthropic)** and **Codex (OpenAI)** usage with color-coded progress bars and desktop
  notifications. Less polished, but covers both providers.
- **CodexBar** (<https://github.com/steipete/codexbar>): usage stats for both OpenAI Codex
  and Claude Code without requiring login. Lightweight; doesn't seem GNOME-specific (more of
  a menu-bar tool).
- **ChatGPT / OpenAI subscription quota**: no dedicated GNOME Shell extension found for the
  "X messages left" type limit. OpenAI's rate-limit and usage APIs don't expose
  subscription-level quota in a way that maps cleanly to a top-bar indicator.

## CLI / Shell Prompt Tools

- **claude-usage** (<https://github.com/phuryn/claude-usage>): local dashboard (TUI) for
  Claude Code token consumption, session costs, and history. Works offline from local
  `~/.claude/` data.
- **coding_agent_usage_tracker**
  (<https://github.com/Dicklesworthstone/coding_agent_usage_tracker>): single CLI supporting
  16+ LLM providers (OpenAI, Anthropic, etc.). Queries provider APIs; needs API keys.
- **openai-token-tracker** (<https://github.com/limonene213u/openai-token-tracker>):
  lightweight CLI for monitoring OpenAI token consumption.
- **Shell prompt integration (Starship / p10k / oh-my-posh)**: **nothing exists off the
  shelf** that displays live AI quota in any of them. Custom segments are feasible: an
  Oh-My-Posh `command` segment in `nix/home/shell/oh-my-posh.nix` that runs a script reading
  `~/.claude/usage/` data and emitting a usage fraction, or a custom `p10k_prompt_segment`
  function in `p10k.zsh` wired into `POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS`. Both need a fast
  data source; the local Claude Code usage files are read-only filesystem access with no
  network round-trip.

Our setup: **GNOME** with extensions via `programs.gnome-shell.extensions` in
`nix/home/home.nix`; **Powerlevel10k** (default) or **Oh-My-Posh** (`USE_OHMYPOSH=1`), both
via home-manager.

## What to watch out for

- **Claude Code quota** (from local files) ≠ **Anthropic API billing quota** (from the API).
  The GNOME extension and most CLI tools track the former (token windows, not invoice limits).
- OpenAI subscription quota ("ChatGPT Plus messages remaining") is not exposed via any public
  API; scraping the web UI is fragile.
