# Karere WhatsApp client, autostarted hidden to the tray. See nix/debug/whatsapp_client.md.
{ pkgs, ... }:
{
  home.packages = [ pkgs.karere ];

  # Default `systray-icon = "auto"` spawns no tray on GNOME, so force it on
  # (shown via the appindicator extension). `start-in-background` is the only way
  # to start hidden; the app has no CLI flag. Leave `run-on-startup` off: it
  # registers autostart imperatively via the XDG Background portal, duplicating
  # the xdg.autostart entry below.
  dconf.settings."io/github/tobagin/karere" = {
    start-in-background = true;
    systray-icon = "enabled";
  };

  xdg.autostart = {
    enable = true;
    entries = [
      (pkgs.writeText "karere.desktop" ''
        [Desktop Entry]
        Type=Application
        Name=Karere
        Exec=karere
        X-GNOME-Autostart-enabled=true
      '')
    ];
  };
}
