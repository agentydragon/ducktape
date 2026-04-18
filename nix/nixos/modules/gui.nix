# GNOME desktop environment
{
  config,
  pkgs,
  lib,
  username,
  ...
}:
{
  imports = [ ./timekpr.nix ];

  # GNOME Desktop
  services.xserver.enable = true;
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;

  # GNOME settings
  services.gnome.gnome-keyring.enable = true;
  programs.dconf.enable = true;

  # Keyring CLI (secret-tool)
  environment.systemPackages = [ pkgs.libsecret ];

  # GSConnect — phone integration via GNOME Shell extension.
  # Uses programs.kdeconnect with the gsconnect package to open the
  # required firewall ports (TCP+UDP 1714-1764). The extension itself is
  # enabled for the user via programs.gnome-shell.extensions in home.nix.
  programs.kdeconnect = {
    enable = true;
    package = pkgs.gnomeExtensions.gsconnect;
  };

  # Screen time management
  ducktape.timekpr = {
    enable = true;
    users.${username} = {
      lockoutType = "lock";
      allowedHours = [
        # 22:00-01:00: allow :00-:50 (forced 10-min break before each hour)
        {
          hours = [
            22
            23
            0
          ];
          minuteRange = "00-50";
        }
        # 01:00-05:00: allow :00-:15 (mostly locked out overnight)
        {
          hours = lib.range 1 4;
          minuteRange = "00-15";
        }
        # 05:00-22:00: full access
        {
          hours = lib.range 5 21;
          minuteRange = "00-60";
        }
      ];
    };
  };
}
