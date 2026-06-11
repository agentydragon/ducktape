# Zsh ZLE Interfaces Used by Inline Autosuggestion Plugins (Summary)

This file lists only the Zsh/ZLE facilities a plugin can rely on to implement inline “ghost text” suggestions, independent of any specific plugin.

## Line editor state (read/write)

- BUFFER (string): full editable command line
- LBUFFER / RBUFFER (string): text left/right of the cursor
- CURSOR (int, 0‑based): insertion point within BUFFER
- POSTDISPLAY / PREDISPLAY (string): extra text shown after/before the cursor (ghost text is typically POSTDISPLAY)
- region_highlight (array): character highlight ranges for coloring; format per `man zshzle` (Character Highlighting)
- KEYMAP, WIDGET (string): current keymap / currently invoked widget name
- PENDING, KEYS_QUEUED_COUNT (int): pending input; skip heavy work when non‑zero

## Widget & keybinding API

- `zle -N <name> <func>`: define user widgets (or wrappers around built‑ins)
- `zle -C <name> <style> <builtin>`: define completion widgets
- `$widgets` (assoc): introspect widget definitions (builtin/user/completion)
- `bindkey …`: bind keys to widgets
- `zle -R` (redisplay), `zle -M` (message): refresh prompt / print status messages

## Editor & shell hooks

- `add-zle-hook-widget` (e.g., `zle-line-init`, `zle-line-finish`): run code at ZLE session boundaries
- `add-zsh-hook` (e.g., `precmd`, `preexec`, `chpwd`, `periodic`): shell‑level hooks useful for setup/teardown

## Asynchronous I/O integration

- `zle -F <fd> <handler>`: register a readable‑FD callback; deliver async suggestion results without blocking typing
- `zmodload zsh/zpty`; `zpty …`: spawn a child on a pty and converse with it (commonly used to query the completion engine safely)
- `zmodload zsh/system`; `$sysparams[…]`: low‑level system parameters when needed
- `zmodload zsh/parameter`: exposes additional special parameters/functions

## Completion system (compsys) affordances

- `compinit`, `_main_complete`, completion widgets (e.g., `.complete-word`)
- `compstate` (assoc): inspect/adjust completion engine state in a completion context
- `comppostfuncs` (array): post‑completion hook to inspect/alter the inserted text
- `zstyle ':completion:*' …`: steer completion behavior for targeted captures

## History access

- `fc -nl …`: efficient read of recent history lines
- `HISTCMD` (int): current history event number
- `history` (assoc param): event‑number → entry text

## Display & styling

- `region_highlight` entries to color specific spans (e.g., the ghost suffix)
- `zle_highlight`/highlight spec (see `man zshzle` → Character Highlighting)

## Typical inline‑suggest flow enabled by these APIs

1. Read line state (`BUFFER`, `CURSOR`, recent history, `$PWD`).
2. Compute suggestion (optionally async via `zle -F`); ensure it begins with `BUFFER`.
3. Render suffix as ghost text using `POSTDISPLAY` and color using `region_highlight`.
4. Provide accept/partial‑accept behavior by binding or wrapping widgets; refresh with `zle -R`.

All of the above are core Zsh/ZLE facilities; no private or plugin‑specific hooks are required.
