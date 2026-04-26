{ pkgs, ... }:
{
  xdg.autostart = {
    enable = true;
    entries = [
      (pkgs.writeText "discord-minimized.desktop" ''
        [Desktop Entry]
        Type=Application
        Name=Discord (Minimized)
        Exec=discord --start-minimized
        Icon=discord
        Terminal=false
        Categories=Network;InstantMessaging;
        X-GNOME-Autostart-enabled=true
      '')
    ];
  };
}
