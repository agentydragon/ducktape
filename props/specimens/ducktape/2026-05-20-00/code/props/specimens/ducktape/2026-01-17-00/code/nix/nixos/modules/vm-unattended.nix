# Auto-login and disabled screen lock for unattended VMs
{
  config,
  pkgs,
  lib,
  username,
  ...
}: {
  # Empty password for VM auto-login - set with `passwd` if needed
  users.users.${username}.initialHashedPassword = "";

  # Auto-login
  services.displayManager.autoLogin = {
    enable = true;
    user = username;
  };

  # Workaround for auto-login with GNOME
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # Disable auto-suspend
  services.displayManager.gdm.autoSuspend = false;

  # Disable screen lock
  programs.dconf.profiles.user.databases = [
    {
      settings = {
        "org/gnome/desktop/session" = {
          idle-delay = lib.gvariant.mkUint32 0;
        };
        "org/gnome/desktop/screensaver" = {
          lock-enabled = false;
          lock-delay = lib.gvariant.mkUint32 0;
        };
      };
    }
  ];
}
