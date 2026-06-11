# Flameshot screenshot configuration
# Configures Flameshot as the default screenshot tool with Print Screen key
{
  # Flameshot autostart desktop entry
  xdg.configFile."autostart/flameshot.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=Flameshot
    Exec=flameshot
    Icon=flameshot
    Terminal=false
    Categories=Graphics;
    X-GNOME-Autostart-enabled=true
  '';

  dconf.settings = {
    # Unbind default GNOME screenshot keys for Flameshot
    "org/gnome/shell/keybindings" = {
      show-screenshot-ui = []; # Was PrnSc
      screenshot = []; # Was Shift+PrnSc
      screenshot-window = []; # Was Alt+PrnSc
    };

    # Flameshot custom keybinding
    "org/gnome/settings-daemon/plugins/media-keys" = {
      custom-keybindings = [
        "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/flameshot-gui/"
      ];
    };

    "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/flameshot-gui" = {
      name = "Flameshot GUI";
      command = "flameshot gui";
      binding = "Print";
    };
  };
}
