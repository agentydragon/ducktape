# Dell Rugged 12 tablet
#
# Manual setup steps:
# - SSH keygen and copy:
#   - GitHub
#   - VPS agentydragon, root
# - Transfer over Ansible Vault password into libwallet
#
# Some setup steps are in Ansible - see ansible/rugged.yaml
#
# TODO: Consider moving some packages from home-manager to system level (zsh, compilers like rustc/go/gcc)
# TODO: SSH authorized_keys - add keys to users.users.agentydragon.openssh.authorizedKeys.keys
# TODO: Improved OSK extension - waiting for GNOME 49 support (currently only 43-44)
# TODO: auto-cpufreq - services.auto-cpufreq for dynamic CPU governor (power saving on battery, performance on AC)
# TODO: zram - consider zramSwap.enable for memory compression (swap file already exists at /swap/swapfile)
# TODO: PipeWire - explicit audio config (services.pipewire with pulse/alsa/jack support)
# TODO: bluetooth group - add to extraGroups if direct bluetooth access needed beyond blueman
{
  config,
  pkgs,
  lib,
  username,
  ...
}: {
  imports = [
    ./hardware-configuration.nix
    ../../modules/gui.nix
    ../../modules/dev-workstation.nix
    ../../modules/system-inspection-sudo.nix
  ];

  # Passwordless sudo for system inspection commands
  ducktape.systemInspectionSudo.enable = true;

  # Timezone
  time.timeZone = "America/Los_Angeles";

  # Bluetooth
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };
  # IIO sensor proxy for accelerometer (auto screen rotation)
  hardware.sensor.iio.enable = true;

  # Services (tailscale enabled via dev-workstation.nix)
  services = {
    blueman.enable = true;
    fwupd.enable = true; # Firmware updates
    printing.enable = true;
    openssh.enable = true;
    thermald.enable = true; # Intel thermal management
    upower.enable = true; # Battery status (dual battery support)
    logind.settings.Login = {
      HandleLidSwitch = "suspend";
      HandleLidSwitchExternalPower = "lock";
      HandlePowerKey = "suspend";
      HandlePowerKeyLongPress = "poweroff";
    };
  };

  # WWAN/5G modem support (Foxconn DP25-42843-47)
  networking.modemmanager.enable = true;
  programs.nm-applet.enable = true;

  hardware.enableAllFirmware = true;

  # System packages
  environment.systemPackages = with pkgs; [
    libsecret # secret-tool for keyring access (used by ansible vault)
    telegram-desktop
  ];

  # Zsh as default shell
  programs.zsh.enable = true;

  # nix-ld: Run dynamically linked binaries (Bazel downloads Python, Rust toolchains, etc.)
  programs.nix-ld.enable = true;

  # User configuration
  users.users.${username} = {
    shell = pkgs.zsh;
    # Allow reading system logs without sudo (systemd-journal group)
    extraGroups = ["systemd-journal"];
  };

  # Allow reading kernel logs without sudo
  boot.kernel.sysctl."kernel.dmesg_restrict" = 0;

  # User groups provided by base.nix: wheel, networkmanager, video, audio
}
