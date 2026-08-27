# GNOME Shell keybindings for Pop Shell on horizontal workspaces.
#
# Pop Shell registers shortcuts through two mechanisms: its own GSettings
# schema (org.gnome.shell.extensions.pop-shell, bound at runtime via
# Main.wm.addKeybinding in src/keybindings.ts), and an installation-time
# configure.sh that clears the GNOME defaults conflicting with them. The nix
# package installs the extension but does NOT run configure.sh; this module
# replicates that conflict resolution via dconf.
#
# Deviations from configure.sh (designed for Pop!_OS vertical workspaces):
# workspace navigation is Super+Arrow (horizontal workspaces, the GNOME 40+
# default, no V-Shell) instead of Super+Ctrl+j/k, and Pop Shell focus keeps
# hjkl only — its arrow variants are stripped to free Super+Arrow.
#
# Iterating: dconf changes from home-manager apply immediately (dconf load
# over the live D-Bus session, which GNOME Shell watches) — no logout needed.
# Keys removed from this module get `dconf reset` on the next switch:
# home-manager tracks the keys it manages in its state file (confirmed
# 2026-03-28).
#
# Gotcha: a nested `dbus-run-session gnome-shell --devkit --wayland` session
# cannot test shortcuts — the outer compositor grabs keybindings before they
# reach the nested instance. Fine for visual/extension testing; test
# shortcuts in the real session.
#
# References:
#   https://github.com/pop-os/shell/blob/master_noble/scripts/configure.sh
#   https://github.com/pop-os/shell/blob/master_noble/schemas/org.gnome.shell.extensions.pop-shell.gschema.xml
{ lib, enableGui, ... }:
let
  emptyStrArray = lib.hm.gvariant.mkEmptyArray lib.hm.gvariant.type.string;
in
lib.mkIf enableGui {
  dconf.settings = {
    # Pop Shell extension preferences and keybinding overrides.
    # Strip arrow key variants from focus (we use Super+Arrow for workspaces
    # instead). Keep only hjkl for window focus navigation.
    # Clear pop-monitor-left/right to free Super+Shift+Left/Right for
    # move-to-workspace.
    "org/gnome/shell/extensions/pop-shell" = {
      gap-inner = lib.hm.gvariant.mkUint32 1;
      gap-outer = lib.hm.gvariant.mkUint32 1;
      tile-by-default = true;
      # Focus: hjkl only (defaults also include Super+Arrow, which we
      # repurpose for workspace switching).
      focus-left = [ "<Super>h" ];
      focus-down = [ "<Super>j" ];
      focus-up = [ "<Super>k" ];
      focus-right = [ "<Super>l" ];
      # Clear monitor-left/right: frees Super+Shift+Left/Right for
      # move-to-workspace-left/right. Monitor up/down (Super+Shift+Ctrl+Up/Down)
      # still works.
      pop-monitor-left = emptyStrArray;
      pop-monitor-right = emptyStrArray;
    };

    # GNOME WM keybindings: clear conflicts with Pop Shell, reassign others.
    #
    # Pop Shell focus (Super+hjkl) conflicts with:
    #   minimize (Super+h), maximize (Super+Up), unmaximize (Super+Down).
    # We repurpose Super+Arrow for workspace switching and Super+Shift+Arrow
    # for moving windows between workspaces.
    "org/gnome/desktop/wm/keybindings" = {
      # Frees Super+h for Pop Shell focus-left.
      minimize = [ "<Super>comma" ];
      # Frees Super+Up for Pop Shell focus-up.
      maximize = emptyStrArray;
      # Frees Super+Down for Pop Shell focus-down.
      unmaximize = emptyStrArray;

      # Workspace switching: Super+Left/Right + Ctrl+Alt+Left/Right.
      switch-to-workspace-left = [
        "<Super>Left"
        "<Control><Alt>Left"
      ];
      switch-to-workspace-right = [
        "<Super>Right"
        "<Control><Alt>Right"
      ];

      # Move window to adjacent workspace.
      move-to-workspace-left = [ "<Super><Shift>Left" ];
      move-to-workspace-right = [ "<Super><Shift>Right" ];
      # Clear up/down variants to avoid conflict with Pop Shell pop-workspace-up/down.
      move-to-workspace-up = emptyStrArray;
      move-to-workspace-down = emptyStrArray;

      # Clear move-to-monitor-* (Pop Shell pop-monitor handles this).
      move-to-monitor-up = emptyStrArray;
      move-to-monitor-down = emptyStrArray;
      move-to-monitor-left = emptyStrArray;
      move-to-monitor-right = emptyStrArray;

      # Maximize reassigned from Super+Up to Super+m.
      toggle-maximized = [ "<Super>m" ];
      # Close window: add Super+q alongside Alt+F4.
      close = [
        "<Super>q"
        "<Alt>F4"
      ];
    };

    # Mutter tiling defaults to Super+Left/Right, which conflicts with our
    # workspace switching. Clear them.
    "org/gnome/mutter/keybindings" = {
      toggle-tiled-left = emptyStrArray;
      toggle-tiled-right = emptyStrArray;
    };

    # Frees Super+Escape for lock screen (reassigned below in media-keys).
    "org/gnome/mutter/wayland/keybindings" = {
      restore-shortcuts = emptyStrArray;
    };

    # Frees Super+m for toggle-maximized and Super+s for Pop Shell toggle-stacking.
    "org/gnome/shell/keybindings" = {
      open-application-menu = emptyStrArray;
      # Frees Super+s for Pop Shell toggle-stacking-global.
      toggle-overview = emptyStrArray;
      # Message tray moved from Super+m to Super+v.
      toggle-message-tray = [ "<Super>v" ];
    };

    # Media key reassignments from configure.sh.
    "org/gnome/settings-daemon/plugins/media-keys" = {
      screensaver = [ "<Super>Escape" ];
      home = [ "<Super>f" ];
      email = [ "<Super>e" ];
      www = [ "<Super>b" ];
      terminal = [ "<Super>t" ];
      # Frees Super+o for Pop Shell tile-orientation.
      rotate-video-lock-static = emptyStrArray;
    };

    # Pop Shell works better with workspaces spanning all displays.
    "org/gnome/mutter" = {
      workspaces-only-on-primary = false;
    };
  };
}
