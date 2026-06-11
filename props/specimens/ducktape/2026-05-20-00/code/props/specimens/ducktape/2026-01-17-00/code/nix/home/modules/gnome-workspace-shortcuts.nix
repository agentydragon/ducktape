# GNOME workspace switching shortcuts configuration
# Configures Ctrl+Alt+Up/Down for workspace switching using GNOME's native vertical shortcuts
{pkgs, ...}: {
  # Note: V-Shell package included but extension disabled to avoid conflicts
  home.packages = with pkgs; [
    gnomeExtensions.vertical-workspaces # ID 5177: V-Shell (available but not enabled)
  ];

  dconf.settings = {
    # Configure enabled extensions to avoid workspace conflicts
    "org/gnome/shell" = {
      disabled-extensions = [
        "vertical-workspaces@G-dH.github.com" # Available but disabled to avoid conflicts
        "cosmic-workspaces@system76.com" # Conflicts with workspace shortcuts
        "pop-cosmic@system76.com" # Causes JS errors with other extensions
      ];
    };

    # Pop!_OS workspace shortcuts workaround
    # Clear Pop!_OS defaults to free up Ctrl+Alt+↑/↓ for GNOME's native shortcuts
    "org/gnome/shell/extensions/pop-shell" = {
      pop-workspace-up = [];
      pop-workspace-down = [];
      pop-monitor-left = [];
      pop-monitor-right = [];
      pop-monitor-up = [];
      pop-monitor-down = [];
    };

    # Clear cosmic-dock conflicting shortcuts that interfere with workspace switching
    "org/gnome/shell/extensions/dash-to-dock" = {
      app-hotkey-1 = [];
      # Clear other potential conflicts if needed
      hot-keys = false;
    };

    # Use GNOME's vertical workspace shortcuts (working configuration)
    "org/gnome/desktop/wm/keybindings" = {
      # Clear horizontal workspace shortcuts
      switch-to-workspace-left = [];
      switch-to-workspace-right = [];
      move-to-workspace-left = [];
      move-to-workspace-right = [];

      # Set vertical workspace shortcuts to Ctrl+Alt+(Shift+)↑/↓
      # This matches the working configuration
      switch-to-workspace-up = ["<Primary><Alt>Up"];
      switch-to-workspace-down = ["<Primary><Alt>Down"];
      move-to-workspace-up = ["<Primary><Shift><Alt>Up"];
      move-to-workspace-down = ["<Primary><Shift><Alt>Down"];
    };
  };
}
