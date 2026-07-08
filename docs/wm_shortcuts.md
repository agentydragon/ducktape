# Window Management Shortcuts (Live System)

Inventory of GNOME window management keybindings on rugged (NixOS, GNOME Shell 49.2), queried from live dconf/gsettings on 2026-03-16.

**Source column legend:**

- **dconf override** = explicitly set in dconf (via Nix home-manager), overriding schema default
- **schema default** = using the GSettings schema default (no dconf entry exists)
- **cleared** = explicitly set to `[]` in dconf, disabling the schema default

## Extension Management Architecture

Extensions are managed via home-manager's `programs.gnome-shell.extensions` module, which automatically:

- Installs extension packages via `home.packages`
- Populates `dconf.settings."org/gnome/shell".enabled-extensions`
- Sets `disable-user-extensions = false`

Extension declarations are distributed across modules (home.nix, solarized.nix, host files). This works because home-manager's `gvariant` type **merges arrays across modules** — when multiple files set `enabled-extensions`, the lists are concatenated (see `modules/lib/types.nix` in home-manager source, the `gvariant` merge function delegates to `listOf` merge for array types).

Extension-specific dconf settings (pop-shell gaps, nightthemeswitcher commands, etc.) remain as raw `dconf.settings` in their respective modules.

## Enabled GNOME Extensions

All managed via `programs.gnome-shell.extensions`.

| Extension                                | Purpose                   | Defined in                               | Status                        |
| ---------------------------------------- | ------------------------- | ---------------------------------------- | ----------------------------- |
| `pop-shell@system76.com`                 | Auto-tiling WM            | `nix/home/home.nix`                      | active                        |
| `vertical-workspaces@G-dH.github.com`    | V-Shell (vertical layout) | `nix/home/home.nix`                      | active                        |
| `panel-date-format@keiii.github.com`     | ISO date in panel         | `nix/home/home.nix`                      | active                        |
| `cronomix@zagortenay333`                 | Timer/clock               | `nix/home/home.nix`                      | v39, OUT OF DATE for GNOME 49 |
| `nightthemeswitcher@romainvigier.fr`     | Auto dark/light           | `nix/home/modules/solarized.nix`         | active                        |
| `appindicatorsupport@rgcjonas.gmail.com` | System tray (NixOS hosts) | `nix/home/hosts/rugged.nix`, `wyrm2.nix` | active                        |

Not enabled: `gjsosk@vishram1123.com` (on-screen keyboard, installed but not in extensions list).

## Pop Shell Settings

dconf path: `/org/gnome/shell/extensions/pop-shell/`

Only these keys have dconf overrides (from `nix/home/modules/gnome-shell-keybindings.nix` and `nix/home/home.nix`):

| Key                         | Live value | Schema default                      | Source         |
| --------------------------- | ---------- | ----------------------------------- | -------------- |
| `tile-by-default`           | `true`     | `false`                             | dconf override |
| `gap-inner`                 | `0`        | `2`                                 | dconf override |
| `gap-outer`                 | `0`        | `2`                                 | dconf override |
| `active-hint-border-radius` | `3`        | `5`                                 | dconf override |
| `pop-workspace-up`          | `[]`       | `['<Super><Shift>Up', ...]`         | cleared        |
| `pop-workspace-down`        | `[]`       | `['<Super><Shift>Down', ...]`       | cleared        |
| `pop-monitor-left`          | `[]`       | `['<Super><Shift>Left', ...]`       | cleared        |
| `pop-monitor-right`         | `[]`       | `['<Super><Shift>Right', ...]`      | cleared        |
| `pop-monitor-up`            | `[]`       | `['<Super><Shift><Ctrl>Up', ...]`   | cleared        |
| `pop-monitor-down`          | `[]`       | `['<Super><Shift><Ctrl>Down', ...]` | cleared        |

All other Pop Shell keybindings are at **schema defaults** (no dconf entry). Schema file: `~/.nix-profile/share/gnome-shell/extensions/pop-shell@system76.com/schemas/org.gnome.shell.extensions.pop-shell.gschema.xml`.

## Pop Shell Keybindings (schema defaults, all active)

### Focus (global, always active)

| Shortcut                                     | Action      | Schema key    | Source         |
| -------------------------------------------- | ----------- | ------------- | -------------- |
| `Super+Left` / `Super+KP_Left` / `Super+h`   | Focus left  | `focus-left`  | schema default |
| `Super+Down` / `Super+KP_Down` / `Super+j`   | Focus down  | `focus-down`  | schema default |
| `Super+Up` / `Super+KP_Up` / `Super+k`       | Focus up    | `focus-up`    | schema default |
| `Super+Right` / `Super+KP_Right` / `Super+l` | Focus right | `focus-right` | schema default |

**Note:** `Super+h` and `Super+l` are intercepted by GNOME (minimize, lock screen) before Pop Shell sees them. See Conflicts section.

### Global Toggles

| Shortcut                          | Action                   | Schema key               | Source         |
| --------------------------------- | ------------------------ | ------------------------ | -------------- |
| `Super+Return` / `Super+KP_Enter` | Enter tiling mode        | `tile-enter`             | schema default |
| `Super+y`                         | Toggle auto-tiling       | `toggle-tiling`          | schema default |
| `Super+g`                         | Toggle floating          | `toggle-floating`        | schema default |
| `Super+o`                         | Toggle orientation       | `tile-orientation`       | schema default |
| `Super+/`                         | Launcher                 | `activate-launcher`      | schema default |
| `Super+s`                         | Toggle stacking (global) | `toggle-stacking-global` | schema default |

### Tiling Mode (inside tiling mode, after `Super+Return`)

| Shortcut                                     | Action             | Schema key               | Source         |
| -------------------------------------------- | ------------------ | ------------------------ | -------------- |
| `Return` / `KP_Enter`                        | Accept             | `tile-accept`            | schema default |
| `Escape`                                     | Reject             | `tile-reject`            | schema default |
| `o`                                          | Toggle orientation | `management-orientation` | schema default |
| `s`                                          | Toggle stacking    | `toggle-stacking`        | schema default |
| `h` / `Left` / `KP_Left`                     | Move left          | `tile-move-left`         | schema default |
| `j` / `Down` / `KP_Down`                     | Move down          | `tile-move-down`         | schema default |
| `k` / `Up` / `KP_Up`                         | Move up            | `tile-move-up`           | schema default |
| `l` / `Right` / `KP_Right`                   | Move right         | `tile-move-right`        | schema default |
| `Shift+h` / `Shift+Left` / `Shift+KP_Left`   | Resize left        | `tile-resize-left`       | schema default |
| `Shift+j` / `Shift+Down` / `Shift+KP_Down`   | Resize down        | `tile-resize-down`       | schema default |
| `Shift+k` / `Shift+Up` / `Shift+KP_Up`       | Resize up          | `tile-resize-up`         | schema default |
| `Shift+l` / `Shift+Right` / `Shift+KP_Right` | Resize right       | `tile-resize-right`      | schema default |
| `Ctrl+h` / `Ctrl+Left` / `Ctrl+KP_Left`      | Swap left          | `tile-swap-left`         | schema default |
| `Ctrl+j` / `Ctrl+Down` / `Ctrl+KP_Down`      | Swap down          | `tile-swap-down`         | schema default |
| `Ctrl+k` / `Ctrl+Up` / `Ctrl+KP_Up`          | Swap up            | `tile-swap-up`           | schema default |
| `Ctrl+l` / `Ctrl+Right` / `Ctrl+KP_Right`    | Swap right         | `tile-swap-right`        | schema default |

### Global Move (disabled by default)

| Schema key               | Live value | Source                 |
| ------------------------ | ---------- | ---------------------- |
| `tile-move-left-global`  | `[]`       | schema default (empty) |
| `tile-move-down-global`  | `[]`       | schema default (empty) |
| `tile-move-up-global`    | `[]`       | schema default (empty) |
| `tile-move-right-global` | `[]`       | schema default (empty) |

## GNOME WM Keybindings (`org.gnome.desktop.wm.keybindings`)

All values from live `gsettings list-recursively`. Source is schema default unless noted.

### Window Actions

| Shortcut                | Action           | gsettings key          | Source         |
| ----------------------- | ---------------- | ---------------------- | -------------- |
| `Super+h`               | **Minimize**     | `minimize`             | schema default |
| `Super+Up`              | Maximize         | `maximize`             | schema default |
| `Super+Down` / `Alt+F5` | Unmaximize       | `unmaximize`           | schema default |
| `Alt+F10`               | Toggle maximized | `toggle-maximized`     | schema default |
| `Alt+F4`                | Close            | `close`                | schema default |
| `Alt+Space`             | Window menu      | `activate-window-menu` | schema default |
| `Alt+F7`                | Begin move       | `begin-move`           | schema default |
| `Alt+F8`                | Begin resize     | `begin-resize`         | schema default |
| `Alt+F2`                | Run dialog       | `panel-run-dialog`     | schema default |

### Application/Window Switching

| Shortcut                                        | Action                  | gsettings key                  | Source         |
| ----------------------------------------------- | ----------------------- | ------------------------------ | -------------- |
| `Super+Tab` / `Alt+Tab`                         | Switch applications     | `switch-applications`          | schema default |
| `Shift+Super+Tab` / `Shift+Alt+Tab`             | Switch apps (reverse)   | `switch-applications-backward` | schema default |
| `Super+Above_Tab` / `Alt+Above_Tab`             | Switch group            | `switch-group`                 | schema default |
| `Shift+Super+Above_Tab` / `Shift+Alt+Above_Tab` | Switch group (reverse)  | `switch-group-backward`        | schema default |
| `Alt+Escape`                                    | Cycle windows           | `cycle-windows`                | schema default |
| `Shift+Alt+Escape`                              | Cycle windows (reverse) | `cycle-windows-backward`       | schema default |
| `Alt+F6`                                        | Cycle group             | `cycle-group`                  | schema default |
| `Shift+Alt+F6`                                  | Cycle group (reverse)   | `cycle-group-backward`         | schema default |
| `Ctrl+Alt+Escape`                               | Cycle panels            | `cycle-panels`                 | schema default |
| `Shift+Ctrl+Alt+Escape`                         | Cycle panels (reverse)  | `cycle-panels-backward`        | schema default |
| `Ctrl+Alt+Tab`                                  | Switch panels           | `switch-panels`                | schema default |
| `Shift+Ctrl+Alt+Tab`                            | Switch panels (reverse) | `switch-panels-backward`       | schema default |

### Workspace Navigation

Configured in `nix/home/modules/gnome-shell-keybindings.nix`. Workspaces are dynamic (`org.gnome.mutter dynamic-workspaces = true`), made **vertical** by V-Shell extension, and only on primary monitor (`workspaces-only-on-primary = true`).

GNOME Shell 49 checks workspace orientation at runtime (`js/ui/windowManager.js:1798-1805`): with `layout_columns === -1` (horizontal, default), only left/right bindings work; with `layout_rows === -1` (vertical, set by V-Shell), only up/down work. V-Shell must be enabled for up/down bindings to function.

| Shortcut              | Action                  | gsettings key              | Source         |
| --------------------- | ----------------------- | -------------------------- | -------------- |
| `Ctrl+Alt+Up`         | Workspace above         | `switch-to-workspace-up`   | dconf override |
| `Ctrl+Alt+Down`       | Workspace below         | `switch-to-workspace-down` | dconf override |
| `Super+Home`          | Workspace 1             | `switch-to-workspace-1`    | dconf override |
| `Super+End`           | Last workspace          | `switch-to-workspace-last` | dconf override |
| `Ctrl+Shift+Alt+Up`   | Move window to ws above | `move-to-workspace-up`     | dconf override |
| `Ctrl+Shift+Alt+Down` | Move window to ws below | `move-to-workspace-down`   | dconf override |
| `Super+Shift+Home`    | Move window to ws 1     | `move-to-workspace-1`      | dconf override |
| `Super+Shift+End`     | Move window to last ws  | `move-to-workspace-last`   | dconf override |

Cleared (set to `[]`): `switch-to-workspace-left`, `switch-to-workspace-right`, `move-to-workspace-left`, `move-to-workspace-right`, `switch-to-workspace-2` through `12`, `move-to-workspace-2` through `12`.

### Move to Monitor

| Shortcut            | Action        | gsettings key           | Source         |
| ------------------- | ------------- | ----------------------- | -------------- |
| `Super+Shift+Down`  | Monitor below | `move-to-monitor-down`  | schema default |
| `Super+Shift+Left`  | Monitor left  | `move-to-monitor-left`  | schema default |
| `Super+Shift+Right` | Monitor right | `move-to-monitor-right` | schema default |
| `Super+Shift+Up`    | Monitor above | `move-to-monitor-up`    | schema default |

### Input Source

| Shortcut                                   | Action                 | gsettings key                  | Source         |
| ------------------------------------------ | ---------------------- | ------------------------------ | -------------- |
| `Super+Space` / `XF86Keyboard`             | Switch input source    | `switch-input-source`          | schema default |
| `Shift+Super+Space` / `Shift+XF86Keyboard` | Switch input (reverse) | `switch-input-source-backward` | schema default |

### Cleared WM Keybindings

These are set to `[]` on live system: `always-on-top`, `lower`, `maximize-horizontally`, `maximize-vertically`, `move-to-center`, `move-to-corner-*`, `move-to-side-*`, `panel-main-menu`, `raise`, `raise-or-lower`, `set-spew-mark`, `show-desktop`, `switch-windows`, `switch-windows-backward`, `toggle-above`, `toggle-fullscreen`, `toggle-on-all-workspaces`.

## Mutter Keybindings (`org.gnome.mutter.keybindings`)

`edge-tiling = false` (Pop Shell handles tiling).

| Shortcut                  | Action               | gsettings key          | Source         |
| ------------------------- | -------------------- | ---------------------- | -------------- |
| `Super+Left`              | Toggle tiled left    | `toggle-tiled-left`    | schema default |
| `Super+Right`             | Toggle tiled right   | `toggle-tiled-right`   | schema default |
| `Super+p` / `XF86Display` | Switch monitor       | `switch-monitor`       | schema default |
| `XF86RotateWindows`       | Rotate monitor       | `rotate-monitor`       | schema default |
| `Super+Shift+Escape`      | Cancel input capture | `cancel-input-capture` | schema default |

## GNOME Shell Keybindings (`org.gnome.shell.keybindings`)

| Shortcut                       | Action                    | gsettings key                   | Source         |
| ------------------------------ | ------------------------- | ------------------------------- | -------------- |
| `Super+a`                      | Toggle application view   | `toggle-application-view`       | schema default |
| `Super+v` / `Super+m`          | Toggle message tray       | `toggle-message-tray`           | schema default |
| `Super+s`                      | Toggle quick settings     | `toggle-quick-settings`         | schema default |
| `Super+n`                      | Focus active notification | `focus-active-notification`     | schema default |
| `Super+Alt+Down`               | Shift overview down       | `shift-overview-down`           | schema default |
| `Super+Alt+Up`                 | Shift overview up         | `shift-overview-up`             | schema default |
| `Ctrl+Shift+Alt+R`             | Screen recording UI       | `show-screen-recording-ui`      | schema default |
| `Super+1`..`Super+9`           | Switch to dock app 1-9    | `switch-to-application-*`       | schema default |
| `Super+Ctrl+1`..`Super+Ctrl+9` | New window for dock app   | `open-new-window-application-*` | schema default |

Cleared: `toggle-overview`, `show-screenshot-ui`, `screenshot`, `screenshot-window` (Flameshot replaces these).

## Custom Keybindings

dconf path: `/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/`

| Shortcut     | Name            | Command          | Source                                       |
| ------------ | --------------- | ---------------- | -------------------------------------------- |
| `Print`      | Flameshot GUI   | `flameshot gui`  | `nix/home/modules/flameshot-screenshots.nix` |
| `Ctrl+Alt+t` | Launch Terminal | `gnome-terminal` | `nix/home/home.nix`                          |

## Conflicts (observed on live system)

Every `Super+<key>` that appears in both Pop Shell schemas and a GNOME/Mutter/media-keys binding is listed below. "Winner" is based on observed behavior where confirmed, or best guess where not yet tested.

| Shortcut      | Pop Shell binding        | GNOME/Mutter/media-keys binding        | Winner (observed)                                                  |
| ------------- | ------------------------ | -------------------------------------- | ------------------------------------------------------------------ |
| `Super+h`     | `focus-left`             | WM `minimize`                          | **GNOME wins** — minimizes window                                  |
| `Super+l`     | `focus-right`            | media-keys `screensaver` (lock screen) | **GNOME wins** — locks screen                                      |
| `Super+o`     | `tile-orientation`       | media-keys `rotate-video-lock-static`  | **Needs testing** — may lock rotation or toggle tiling orientation |
| `Super+s`     | `toggle-stacking-global` | Shell `toggle-quick-settings`          | **Needs testing**                                                  |
| `Super+Left`  | `focus-left`             | Mutter `toggle-tiled-left`             | Pop Shell wins                                                     |
| `Super+Right` | `focus-right`            | Mutter `toggle-tiled-right`            | Pop Shell wins                                                     |
| `Super+Up`    | `focus-up`               | WM `maximize`                          | Pop Shell wins                                                     |
| `Super+Down`  | `focus-down`             | WM `unmaximize`                        | Pop Shell wins                                                     |
| `Alt+Super+s` | —                        | media-keys `screenreader`              | No Pop Shell conflict, but note for `Super+s` proximity            |

### Additional media-keys on `Super+` combos (no Pop Shell conflict)

These don't conflict with Pop Shell but are worth knowing about:

| Shortcut      | Action             | gsettings key              | Source         |
| ------------- | ------------------ | -------------------------- | -------------- |
| `Super+l`     | Lock screen        | `screensaver`              | schema default |
| `Super+o`     | Rotate video lock  | `rotate-video-lock-static` | schema default |
| `Super+F1`    | Help               | `help`                     | schema default |
| `Alt+Super+8` | Magnifier          | `magnifier`                | schema default |
| `Alt+Super+=` | Magnifier zoom in  | `magnifier-zoom-in`        | schema default |
| `Alt+Super+-` | Magnifier zoom out | `magnifier-zoom-out`       | schema default |
| `Alt+Super+s` | Screen reader      | `screenreader`             | schema default |

## Configuration Source Files

| File                                             | What it configures                                                       |
| ------------------------------------------------ | ------------------------------------------------------------------------ |
| `nix/home/home.nix`                              | `programs.gnome-shell.extensions` (shared), Pop Shell settings, terminal |
| `nix/home/modules/solarized.nix`                 | Night Theme Switcher extension + dconf settings                          |
| `nix/home/modules/gnome-shell-keybindings.nix`   | Workspace nav keybindings, clears Pop Shell workspace/monitor keys      |
| `nix/home/modules/gnome-custom-keybindings.nix`  | Custom keybinding framework                                              |
| `nix/home/modules/flameshot-screenshots.nix`     | Print Screen -> Flameshot                                                |
| `nix/home/hosts/rugged.nix`, `wyrm2.nix`         | Appindicator extension (NixOS hosts)                                     |
| Pop Shell gschema XML                            | Default keybindings for all Pop Shell actions                            |
