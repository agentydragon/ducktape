# Disable screensaver, screen blanking, and auto-lock.
# Intended for VMs and headless workstations.
{ lib, ... }:
{
  dconf.settings = {
    "org/gnome/desktop/screensaver" = {
      idle-activation-enabled = false;
      lock-enabled = false;
    };
    "org/gnome/desktop/session" = {
      idle-delay = lib.hm.gvariant.mkUint32 0;
    };
    "org/gnome/settings-daemon/plugins/power" = {
      idle-dim = false;
      sleep-inactive-ac-type = "nothing";
      sleep-inactive-battery-type = "nothing";
    };
  };
}
