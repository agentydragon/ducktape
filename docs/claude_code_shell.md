# Claude Code Shell Integration

## Environment Detection

Claude Code CLI sets the `CLAUDECODE=1` environment variable when running commands through its Bash tool.

```bash
# Check if running in Claude Code context
if [[ -n "$CLAUDECODE" ]]; then
    echo "Running in Claude Code CLI"
fi
```

### Additional Claude Environment Variables

- `CLAUDE_CODE_ENTRYPOINT=cli` - Indicates the entry point used
- `CLAUDECODE=1` - Primary detection flag
- `GIT_EDITOR=true` - Prevents git from opening interactive editors
- `SHELL=/path/to/shell` - Set to the detected shell path

## The Zoxide Problem

### Why Zoxide Breaks in Claude Code

Claude Code's shell snapshotting system filters out functions that start with `_` or `__`, treating them as "system/private" functions. This breaks zoxide because:

1. **Your Interactive Shell**: Has `cd()` function and `__zoxide_z()` function
2. **Claude's Snapshot Process**:
   - ✅ Captures `cd()` (doesn't start with `_`)
   - ❌ **Filters out `__zoxide_z()`** (starts with `__`)
3. **Claude's Execution**:
   - Sources snapshot with `cd()` but missing `__zoxide_z()`
   - Result: `cd` → `__zoxide_z "$@"` → `command not found`

### Shell Snapshot Implementation Details

Claude creates a shell script that enumerates functions with explicit filtering:

**For Zsh:**

```bash
# Get user function names - FILTER OUT system ones
typeset +f | grep -vE '^(_|__)' | while read func; do
  typeset -f "$func" >> "$SNAPSHOT_FILE"
done
```

**The Critical Filter**: `grep -vE '^(_|__)'` **explicitly excludes any function starting with `_` or `__`**

This architectural decision breaks modern shell tools that use private/public function patterns.

## Workarounds

### Option 1: Conditional Integration (Recommended)

Detect Claude Code environment during shell initialization and skip zoxide:

```zsh
# Only initialize zoxide when NOT running in Claude Code
if [[ -z "$CLAUDECODE" ]]; then
    eval "$(zoxide init zsh --cmd cd)"
fi
```

**Note**: This detection happens during shell startup, not during Claude's snapshot phase, so it works correctly.

### Option 2: Subshell Wrapper

```zsh
claude() {
    # Run claude in a subshell with problematic functions removed
    (
        unfunction cd 2>/dev/null || true
        command claude "$@"
    )
}
```

### Option 3: Alternative Commands

When Claude environment is detected, use alternatives:

- `builtin cd` instead of `cd`
- `pushd` instead of `cd`
- `command cd` to bypass function

## Implementation Status

✅ **Nix/Home-Manager systems** have the workaround implemented in [`nix/home/home.nix`](../nix/home/home.nix#L839):

```nix
# Conditional zoxide integration for Claude Code compatibility
# Only initialize zoxide when NOT running in Claude Code to prevent function
# definition conflicts. Claude Code filters out functions starting with '_' or '__',
# breaking zoxide's __zoxide_z() function which cd() depends on.
(lib.mkOrder 1400 ''
  if [[ -z "$CLAUDECODE" ]]; then
    eval "$(${lib.getExe pkgs.zoxide} init zsh --cmd cd)"
  fi
'')
```

❌ **Legacy systems** (gpd, vps) may still need manual workarounds in their shell configurations.
