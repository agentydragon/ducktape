# AI Subscription Quota Trackers — Research Notes

## GNOME Shell Extensions

### Claude Code Usage

- **URL**: <https://extensions.gnome.org/extension/9231/claude-code-usage/>
- **Source**: <https://github.com/xguitoux/ClaudeCodeUsage-GnomeExtension>
- Shows **Claude Code** 5-hour and 7-day token quota percentages in the GNOME top bar.
- Reads local Claude Code usage data (not live Anthropic API calls).
- Could be added to `nix/home/home.nix` via `programs.gnome-shell.extensions`.

### brainusage

- **Source**: <https://github.com/AltairInglorious/brainusage>
- Tracks both **Claude (Anthropic)** and **Codex (OpenAI)** usage with color-coded progress bars and desktop notifications.
- Less polished, but covers both providers.

### CodexBar

- **Source**: <https://github.com/steipete/codexbar>
- Shows usage stats for both OpenAI Codex and Claude Code without requiring login.
- Lightweight; doesn't seem GNOME-specific (more of a menu-bar tool).

### ChatGPT / OpenAI subscription quota

- No dedicated GNOME Shell extension found for **ChatGPT subscription quota** (the "X messages left" type limit).
- OpenAI's rate-limit and usage APIs don't expose subscription-level quota in a way that maps cleanly to a top-bar indicator.

---

## CLI / Shell Prompt Tools

### claude-usage

- **Source**: <https://github.com/phuryn/claude-usage>
- Local dashboard (TUI) for Claude Code token consumption, session costs, and history.
- Works offline from local `~/.claude/` data.

### coding_agent_usage_tracker

- **Source**: <https://github.com/Dicklesworthstone/coding_agent_usage_tracker>
- Single CLI supporting 16+ LLM providers (OpenAI, Anthropic, etc.).
- Queries provider APIs; needs API keys.

### openai-token-tracker

- **Source**: <https://github.com/limonene213u/openai-token-tracker>
- Lightweight CLI for monitoring OpenAI token consumption.

### Shell prompt integration (Starship / p10k / oh-my-posh)

- **Nothing exists off the shelf** for any of Starship, Powerlevel10k, or Oh-My-Posh that displays live AI quota.
- Custom segments are feasible (see integration notes below).

---

## Integration with Your Setup

Your setup uses:

- **GNOME** with extensions managed via `programs.gnome-shell.extensions` in `nix/home/home.nix`.
- **Powerlevel10k** (default) or **Oh-My-Posh** (`USE_OHMYPOSH=1`), both via home-manager.

### Adding a GNOME extension

The Claude Code Usage extension is the most polished option. Add it to `nix/home/home.nix`:

```nix
programs.gnome-shell = {
  extensions = [
    # ... existing entries ...
    { package = pkgs.gnomeExtensions.claude-code-usage; }
  ];
};
```

Check if it's in `nixpkgs` first: `nix search nixpkgs claude-code-usage`. If not, you'd need to package it manually via `pkgs.buildEnv` / `fetchFromGitHub` in an overlay.

### Adding a prompt segment

For **Oh-My-Posh**, a `command` segment can run an arbitrary shell command and display its output. You could add a segment to `nix/home/shell/oh-my-posh.nix` that calls a script reading `~/.claude/usage/` data and emitting a usage fraction.

For **Powerlevel10k**, add a custom `p10k_prompt_segment` function in `p10k.zsh` and wire it into `POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS`.

Both approaches require a fast data source — the local Claude Code usage files are suitable since they're read-only filesystem access with no network round-trip.

### What to watch out for

- **Claude Code quota** (from local files) ≠ **Anthropic API billing quota** (from the API). The GNOME extension and most CLI tools track the former (token windows, not invoice limits).
- OpenAI subscription quota ("ChatGPT Plus messages remaining") is not exposed via any public API; scraping the web UI is fragile.
