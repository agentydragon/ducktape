# Git Hook Chaining

Run custom hooks before the normal hook path, without breaking existing hook
setups (pre-commit, etc.).

## Problem

`core.hooksPath` is a single value — setting it replaces the original. A wrapper
needs to capture the original path before overriding it.

## Solution

### Git wrapper (`~/bin/git`)

Captures the current `core.hooksPath`, passes it via env var, then overrides
with the dispatcher directory:

```bash
#!/bin/sh
ORIGINAL_HOOKS_PATH=$(command git config core.hooksPath 2>/dev/null)
export GIT_ORIGINAL_HOOKS_PATH="${ORIGINAL_HOOKS_PATH:-.git/hooks}"

exec command git \
  -c core.hooksPath="$HOME/.config/git-hooks" \
  "$@"
```

### Dispatcher (`~/.config/git-hooks/<hook-name>`)

Each hook (e.g., `pre-commit`, `pre-push`) is a dispatcher that runs custom
hooks first, then chains to the original:

```bash
#!/bin/sh
HOOK_NAME=$(basename "$0")

# Custom hooks first
for hook in "$HOME/.config/git-hooks/custom.d/$HOOK_NAME"/*; do
    [ -x "$hook" ] && "$hook" "$@" || exit $?
done

# Chain to original
original="${GIT_ORIGINAL_HOOKS_PATH:-.git/hooks}/$HOOK_NAME"
[ -x "$original" ] && exec "$original" "$@"
```

### Custom hooks (`~/.config/git-hooks/custom.d/<hook-name>/`)

Drop executable scripts here. They run in lexicographic order. Any non-zero
exit aborts the chain (remaining custom hooks and the original hook are skipped).

## Why this works

- The env var `GIT_ORIGINAL_HOOKS_PATH` captures whatever `core.hooksPath` was
  before the wrapper overrides it (could be `.git/hooks`, could be pre-commit's
  directory, etc.).
- The dispatcher runs custom hooks first, then `exec`s the original — so
  pre-commit or any other hook manager still works as the final step.
- Composable: if nothing else set `core.hooksPath`, it falls back to `.git/hooks`.
