# GNOME desktop environment
{
  config,
  pkgs,
  lib,
  ...
}: {
  # GNOME Desktop
  services.xserver.enable = true;
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;

  # GNOME settings
  services.gnome.gnome-keyring.enable = true;
  programs.dconf.enable = true;
}
