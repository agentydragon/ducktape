# GNOME Workspace Shortcuts Investigation

Goal: get clean, conflict-free keyboard shortcuts for workspace/monitor
navigation with Pop Shell on GNOME 49 Wayland (horizontal workspaces).

Branch: `gnome-shortcuts`

## Current Keybinding Layout

Implemented in `nix/home/modules/gnome-shell-keybindings.nix`. Based on
Pop Shell's `configure.sh` but adapted for horizontal workspaces (GNOME 40+
default, no V-Shell). `configure.sh` was designed for Pop!\_OS vertical
workspaces — we deviate for workspace navigation.

### Window Focus (Pop Shell, hjkl only)

| Shortcut  | Action      |
| --------- | ----------- |
| `Super+h` | Focus left  |
| `Super+j` | Focus down  |
| `Super+k` | Focus up    |
| `Super+l` | Focus right |

Arrow variants stripped from Pop Shell defaults to free `Super+Arrow` for
workspace switching.

### Workspace Navigation (GNOME WM)

| Shortcut              | Action                          |
| --------------------- | ------------------------------- |
| `Super+Left`          | Switch workspace left           |
| `Super+Right`         | Switch workspace right          |
| `Ctrl+Alt+Left`       | Switch workspace left (classic) |
| `Ctrl+Alt+Right`      | Switch workspace right          |
| `Super+Shift+Left`    | Move window to workspace left   |
| `Super+Shift+Right`   | Move window to workspace right  |
| `Super+Shift+Up/Down` | Pop Shell pop-workspace up/down |

### Window Management

| Shortcut       | Action             | Source        |
| -------------- | ------------------ | ------------- |
| `Super+Return` | Enter tiling mode  | Pop Shell     |
| `Super+m`      | Toggle maximized   | Reassigned    |
| `Super+comma`  | Minimize           | Reassigned    |
| `Super+q`      | Close window       | configure.sh  |
| `Alt+F4`       | Close window       | GNOME default |
| `Super+y`      | Toggle auto-tiling | Pop Shell     |
| `Super+g`      | Toggle floating    | Pop Shell     |
| `Super+o`      | Toggle orientation | Pop Shell     |
| `Super+s`      | Toggle stacking    | Pop Shell     |
| `Super+/`      | Launcher           | Pop Shell     |

### System (media-keys, from configure.sh)

| Shortcut       | Action        |
| -------------- | ------------- |
| `Super+Escape` | Lock screen   |
| `Super+f`      | File manager  |
| `Super+e`      | Email         |
| `Super+b`      | Browser       |
| `Super+t`      | Terminal      |
| `Super+v`      | Notifications |

### Monitor Movement (Pop Shell)

| Shortcut                  | Action                      |
| ------------------------- | --------------------------- |
| `Super+Shift+Ctrl+Up/k`   | Move window to monitor up   |
| `Super+Shift+Ctrl+Down/j` | Move window to monitor down |

`pop-monitor-left/right` cleared to free `Super+Shift+Left/Right` for
workspace movement. Only Ctrl variants remain for monitor movement.

### Tiling Mode Only (after `Super+Return`)

| Shortcut           | Action             |
| ------------------ | ------------------ |
| `Arrow/hjkl`       | Move window        |
| `Shift+Arrow/hjkl` | Resize window      |
| `Ctrl+Arrow/hjkl`  | Swap window        |
| `o`                | Toggle orientation |
| `s`                | Toggle stacking    |
| `Return`           | Accept             |
| `Escape`           | Reject             |

## Iteration Workflow

### Deploy cycle

dconf changes from home-manager take effect **immediately** — no logout
needed. Home-manager runs `dconf load` through the existing D-Bus session,
and GNOME Shell watches dconf via D-Bus signals in real-time.

Home-manager also tracks managed keys in a state file. Keys removed from
config get `dconf reset` on next switch (confirmed: Scenario A in
predictions.md).

```bash
# 1. Edit nix config
vim nix/home/modules/gnome-shell-keybindings.nix

# 2. Deploy — shortcuts are live immediately
home-manager switch --flake ~/code/ducktape#agentydragon
# or if system-level changes needed:
# sudo nixos-rebuild switch --flake '.#rugged'

# 3. Verify dconf state
dconf dump /org/gnome/desktop/wm/keybindings/

# 4. Test shortcuts directly in the running session
```

### For visual/extension testing (NOT keyboard shortcuts)

GNOME devkit mode runs a windowed GNOME Shell:

```bash
dbus-run-session gnome-shell --devkit --wayland
```

**Cannot test keyboard shortcuts** — the outer Wayland compositor grabs
keybindings at the compositor level before they reach the nested instance.

## How Pop Shell Registers Shortcuts

Pop Shell uses two mechanisms:

### 1. Own GSettings schema (runtime)

All Pop Shell shortcuts live in `org.gnome.shell.extensions.pop-shell`
(defined in `schemas/org.gnome.shell.extensions.pop-shell.gschema.xml`).
At runtime, `keybindings.ts` calls `Main.wm.addKeybinding()` with
`Shell.ActionMode.NORMAL` for each shortcut. Two sets:

- **Global** (always active): `focus-*`, `tile-enter`, `toggle-tiling`,
  `toggle-floating`, `tile-orientation`, `toggle-stacking-global`,
  `activate-launcher`, `pop-workspace-*`, `pop-monitor-*`
- **Tiling mode** (only inside `Super+Return` mode): `tile-move-*`,
  `tile-resize-*`, `tile-swap-*`, `tile-accept`, `tile-reject`

### 2. `configure.sh` (installation-time dconf overrides)

`scripts/configure.sh` clears conflicting GNOME shortcuts before Pop Shell
starts. Designed for Pop!\_OS **vertical** workspaces — we adapt for
horizontal. See `gnome-shell-keybindings.nix` header for full reference links.

**Key deviation from `configure.sh`**: it assigns workspace switching to
`Super+Ctrl+j/k` (vertical). We use `Super+Left/Right` (horizontal) and
strip arrow variants from Pop Shell focus (hjkl only).

## Key Files

| File                                           | Purpose                                       |
| ---------------------------------------------- | --------------------------------------------- |
| <nix/home/modules/gnome-shell-keybindings.nix> | Our dconf shortcut config                     |
| <nix/home/home.nix>                            | Extension list                                |
| <debug/gnome-shortcuts/predictions.md>         | Experiment predictions and results log        |
| <wm-shortcuts.md>                              | Live system keybinding inventory (2026-03-16) |

### Pop Shell source (cloned at `~/code/pop-shell`, branch `master_noble`)

| File                                                       | Purpose                                     |
| ---------------------------------------------------------- | ------------------------------------------- |
| `schemas/org.gnome.shell.extensions.pop-shell.gschema.xml` | All shortcut defaults                       |
| `scripts/configure.sh`                                     | GNOME conflict resolution                   |
| `src/keybindings.ts`                                       | Runtime `addKeybinding()` calls             |
| `src/extension.ts`                                         | Enable/disable, workspace/monitor move impl |
