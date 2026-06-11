# Predictions: Disable V-Shell + Remove KB Overrides

Date: 2026-03-23
Branch: `gnome-shortcuts`

## Changes Made

1. **Removed `vertical-workspaces` (V-Shell)** from `programs.gnome-shell.extensions` in `home.nix`
2. **Stripped `gnome-workspace-shortcuts.nix`** to only Pop Shell tiling prefs (`gap-inner=1`, `gap-outer=1`, `tile-by-default=true`). Removed all dconf overrides:
   - WM keybinding overrides (workspace up/down/left/right, move-to-workspace)
   - Pop Shell shortcut clears (`pop-workspace-*`, `pop-monitor-*`)
   - Dash-to-dock hotkey clears

## Key Uncertainty: dconf Reset Behavior

Home-manager may or may not reset dconf keys that were previously set but are now removed from config. Two scenarios:

### Scenario A: Home-manager resets removed keys (expected for recent versions)

All overrides revert to schema defaults. Predicted state:

**Workspace layout**: Horizontal (GNOME 40+ default without V-Shell).

**GNOME WM keybindings** (schema defaults for GNOME 49):

| Shortcut                | Action                 | Notes                                           |
| ----------------------- | ---------------------- | ----------------------------------------------- |
| `Ctrl+Alt+Left`         | Switch workspace left  | Schema default (was previously overridden)      |
| `Ctrl+Alt+Right`        | Switch workspace right | Schema default (was previously cleared to `[]`) |
| `Super+Up`              | Maximize               | Schema default                                  |
| `Super+Down` / `Alt+F5` | Unmaximize             | Schema default                                  |
| `Super+h`               | Minimize               | Schema default                                  |
| `Alt+F4`                | Close                  | Schema default                                  |
| `Super+Tab` / `Alt+Tab` | Switch applications    | Schema default                                  |

**Pop Shell keybindings** (all restored to schema defaults):

| Shortcut                | Action             | Previously              |
| ----------------------- | ------------------ | ----------------------- |
| `Super+Left/h`          | Focus left         | Was active (default)    |
| `Super+Right/l`         | Focus right        | Was active (default)    |
| `Super+Up/k`            | Focus up           | Was active (default)    |
| `Super+Down/j`          | Focus down         | Was active (default)    |
| `Super+Return`          | Enter tiling mode  | Was active (default)    |
| `Super+y`               | Toggle auto-tiling | Was active (default)    |
| `Super+g`               | Toggle floating    | Was active (default)    |
| `Super+o`               | Toggle orientation | Was active (default)    |
| `Super+/`               | Launcher           | Was active (default)    |
| `Super+s`               | Toggle stacking    | Was active (default)    |
| `Super+Shift+Up`        | Pop workspace up   | **Was cleared to `[]`** |
| `Super+Shift+Down`      | Pop workspace down | **Was cleared to `[]`** |
| `Super+Shift+Left`      | Pop monitor left   | **Was cleared to `[]`** |
| `Super+Shift+Right`     | Pop monitor right  | **Was cleared to `[]`** |
| `Super+Shift+Ctrl+Up`   | Pop monitor up     | **Was cleared to `[]`** |
| `Super+Shift+Ctrl+Down` | Pop monitor down   | **Was cleared to `[]`** |

**Known conflicts** (all at schema defaults, no resolution applied):

| Shortcut      | Pop Shell        | GNOME                          | Expected winner                 |
| ------------- | ---------------- | ------------------------------ | ------------------------------- |
| `Super+h`     | Focus left       | Minimize                       | GNOME (intercepts first)        |
| `Super+l`     | Focus right      | Lock screen (media-keys)       | GNOME (intercepts first)        |
| `Super+s`     | Toggle stacking  | Quick settings (Shell)         | Needs testing                   |
| `Super+o`     | Tile orientation | Rotate video lock (media-keys) | Needs testing                   |
| `Super+Up`    | Focus up         | Maximize (WM)                  | Pop Shell (observed previously) |
| `Super+Down`  | Focus down       | Unmaximize (WM)                | Pop Shell (observed previously) |
| `Super+Left`  | Focus left       | Toggle tiled left (Mutter)     | Pop Shell (observed previously) |
| `Super+Right` | Focus right      | Toggle tiled right (Mutter)    | Pop Shell (observed previously) |

### Scenario B: Home-manager does NOT reset removed keys (stale dconf persists)

Old dconf overrides remain. Predicted state:

**Broken workspace navigation**:

- `switch-to-workspace-left` = `[]` (stale clear)
- `switch-to-workspace-right` = `[]` (stale clear)
- `switch-to-workspace-up` = `['<Primary><Alt>Up']` (stale override, won't work without V-Shell)
- `switch-to-workspace-down` = `['<Primary><Alt>Down']` (stale override, won't work without V-Shell)
- Pop Shell `pop-workspace-*` still cleared to `[]`
- **Result: no workspace switching works at all**

**How to detect**: After `home-manager switch`, before logout:

```bash
dconf read /org/gnome/desktop/wm/keybindings/switch-to-workspace-left
```

If it shows `@as []`, we're in Scenario B.

**Fix for Scenario B**:

```bash
dconf reset -f /org/gnome/desktop/wm/keybindings/
dconf reset -f /org/gnome/shell/extensions/pop-shell/
dconf reset -f /org/gnome/shell/extensions/dash-to-dock/
```

## Results (2026-03-28)

Deployed with `sudo nixos-rebuild switch --flake '.#rugged'`, tested via
`dbus-run-session gnome-shell --devkit --wayland`.

**Scenario A confirmed**: `dconf read .../switch-to-workspace-left` returned
empty (schema default), not `@as []`. Home-manager resets removed keys.

**Observed:**

- Workspace layout: horizontal (side-by-side in overview) — correct
- `Ctrl+Alt+Left/Right`: switches workspaces on the **outer** session, not devkit
- `Super+Left/Right`: moves windows on the **outer** session
- `Super+h`: minimizes the devkit window (outer GNOME intercepts)
- `Super+s`: stacking toggle on outer session

**Devkit limitation discovered**: the outer Wayland compositor grabs
keybindings at the compositor level before they reach the nested instance.
Devkit is useful for visual/extension testing but **cannot test keyboard
shortcuts** — all Super/Ctrl+Alt combos are intercepted by the host mutter.

**Viable testing approaches for shortcuts:**

1. Test on real session (log out/in after `home-manager switch`)
2. Reason from dconf state + schema knowledge (sufficient for conflict analysis)
3. Separate VT (`Ctrl+Alt+F3`, full GNOME session on separate display)

## configure.sh Replication (2026-03-28)

Deployed `gnome-shell-keybindings.nix` (replaces `gnome-workspace-shortcuts.nix`).
All dconf values verified correct:

| dconf key                                      | Expected                                        | Actual  |
| ---------------------------------------------- | ----------------------------------------------- | ------- |
| `wm/keybindings/minimize`                      | `['<Super>comma']`                              | correct |
| `wm/keybindings/maximize`                      | `@as []`                                        | correct |
| `wm/keybindings/unmaximize`                    | `@as []`                                        | correct |
| `wm/keybindings/toggle-maximized`              | `['<Super>m']`                                  | correct |
| `wm/keybindings/switch-to-workspace-left`      | `@as []`                                        | correct |
| `wm/keybindings/switch-to-workspace-right`     | `@as []`                                        | correct |
| `wm/keybindings/switch-to-workspace-up`        | `['<Primary><Super>Up', '<Primary><Super>k']`   | correct |
| `wm/keybindings/switch-to-workspace-down`      | `['<Primary><Super>Down', '<Primary><Super>j']` | correct |
| `wm/keybindings/move-to-monitor-left`          | `@as []`                                        | correct |
| `wm/keybindings/move-to-workspace-up`          | `@as []`                                        | correct |
| `wm/keybindings/close`                         | `['<Super>q', '<Alt>F4']`                       | correct |
| `mutter/keybindings/toggle-tiled-left`         | `@as []`                                        | correct |
| `mutter/keybindings/toggle-tiled-right`        | `@as []`                                        | correct |
| `mutter/wayland/keybindings/restore-shortcuts` | `@as []`                                        | correct |
| `mutter/workspaces-only-on-primary`            | `false`                                         | correct |
| `shell/keybindings/toggle-overview`            | `@as []`                                        | correct |
| `shell/keybindings/open-application-menu`      | `@as []`                                        | correct |
| `shell/keybindings/toggle-message-tray`        | `['<Super>v']`                                  | correct |
| `media-keys/screensaver`                       | `['<Super>Escape']`                             | correct |
| `media-keys/terminal`                          | `['<Super>t']`                                  | correct |
| `media-keys/rotate-video-lock-static`          | `@as []`                                        | correct |

**Status**: dconf state matches plan.

## Live Testing Round 2 (2026-03-28)

After fixing horizontal workspace navigation and mutter tiling conflicts:

- `Super+Left/Right`: switches workspaces left/right — **working**
- `Super+Shift+Left/Right`: moves window between workspaces — **working**
- `Ctrl+Alt+Left/Right`: switches workspaces — **working**
- `Super+hjkl`: focuses windows — **working**
- `Super+m`: toggle maximized — **working**

All target shortcuts confirmed working.
