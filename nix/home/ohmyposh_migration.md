# Oh-My-Posh Migration from Powerlevel10k

**Date:** 2026-01-16
**Working Directory:** `/home/agentydragon/code/ducktape/nix/home`
**Git Branch:** `devel`

## What We Accomplished

### Prompt Generator Selection

Evaluated alternatives to Powerlevel10k for shell prompts. Key finding: **Starship cannot properly handle powerline separators with conditional segments** - when a conditional segment is hidden, the color chain breaks (see [starship#6218](https://github.com/starship/starship/issues/6218)).

**Oh-My-Posh selected** because it computes separator colors at render time based on `previousActiveSegment`, correctly handling hidden conditional segments.

### Files Modified

- **`nix/home/ohmyposh.json`** (created): Oh-My-Posh configuration
  - Session segment with SSH-conditional display (line 17): `{{ if .SSHSession }}{{ .UserName }}@{{ .HostName }} {{ end }}`
  - Path with agnoster_short style
  - Git segment with status-based background colors
  - Right prompt: status, sudo, execution time, nix-shell, time
  - Transient prompt enabled (lines 110-112)

- **`nix/home/shell/zsh-init.zsh`** (modified lines 5-16):
  - Added `USE_OHMYPOSH` env var toggle between p10k and Oh-My-Posh
  - Added `unset RPROMPT` fix before oh-my-posh init (fixes incrementing integer bug)

- **`nix/home/home.nix`** (modified):
  - Added `oh-my-posh` to packages
  - Added xdg.configFile for oh-my-posh config
  - Kept p10k packages/config for toggle functionality

### Bugs Fixed

1. **Incrementing integer on right prompt**: Caused by `RPROMPT = "%*"` in home.nix sessionVariables conflicting with Oh-My-Posh's rprompt. Fixed by `unset RPROMPT` in zsh-init.zsh.

2. **Session segment always showing user@hostname**: Diagnosed as **stale cache** issue (see below).

## Incomplete Work / Open Threads

### Oh-My-Posh Cache Architecture Problem (HIGH PRIORITY)

**Root cause analysis of user@hostname appearing unexpectedly:**

The Oh-My-Posh cache has a design flaw in config keying:

From `/code/github.com/JanDeDobbeleer/oh-my-posh/src/cache/init.go:49-50`:

```go
sessionFileName := fmt.Sprintf("%s.%s.%s", shell, SessionID(), DeviceStore)
Session.init(sessionFileName, persist)
```

From `/code/github.com/JanDeDobbeleer/oh-my-posh/src/config/gob.go:37-40`:

```go
base64String, found := cache.Get[string](cache.Session, configKey)
// configKey is just "CONFIG" - no path in key
```

**The bug:** Config is cached under key `CONFIG` per session ID, **not keyed by config file path**. If:

1. Shell A starts with config X → cached as `CONFIG`
2. Shell B (inheriting `POSH_SESSION_ID`) starts with config Y → **reads cached config X**

**Workaround applied:** `rm -rf ~/.cache/oh-my-posh/*` clears stale cache.

**Potential permanent fixes:**

1. Key config cache by file path hash
2. Store config file mtime and invalidate on change
3. Include config path in session file name

### Files Not Yet Committed

```
M  nix/home/ohmyposh.json
M  nix/home/shell/zsh-init.zsh
```

The plan file at `~/.claude/plans/moonlit-dazzling-gadget.md` documents the full migration plan and was updated during the session.

## Context for Successor Agents

### Project Conventions

- See: `@AGENTS.md` for repository conventions
- Shell config managed by Nix home-manager (`nix/home/home.nix`)
- Shell init scripts in `nix/home/shell/`

### Build/Test

```bash
# Rebuild home-manager (on wyrm needs --impure for nixGL)
home-manager switch --flake .#agentydragon --impure

# Test oh-my-posh prompt
USE_OHMYPOSH=1 zsh -c 'source ~/.zshrc; oh-my-posh print primary --config ~/.config/oh-my-posh/config.json'

# Clear oh-my-posh cache if stale config issues
rm -rf ~/.cache/oh-my-posh/*
```

### Key Constraints

- `USE_OHMYPOSH=1` env var enables Oh-My-Posh; otherwise defaults to Powerlevel10k
- On wyrm, `home-manager switch` requires `--impure` flag for nixGL
- Oh-My-Posh session segment checks `SSH_CONNECTION`, `SSH_CLIENT` env vars, then `who am i` for IP pattern

### Oh-My-Posh Source Code Reference

Cloned to `/code/github.com/JanDeDobbeleer/oh-my-posh/`:

- `src/segments/session.go` - SSH detection logic, default template
- `src/config/segment.go:221` - Segment disabled if template output empty
- `src/config/gob.go` - Config caching logic
- `src/cache/init.go` - Session ID and cache file naming

## Related Documentation

- Plan file: `~/.claude/plans/moonlit-dazzling-gadget.md`
- Oh-My-Posh docs: https://ohmyposh.dev/docs/
- Starship issue on powerline separators: https://github.com/starship/starship/issues/6218
